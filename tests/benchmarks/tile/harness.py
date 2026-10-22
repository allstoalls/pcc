"""tests/benchmarks/tile/harness.py — metadata-only Tile/TIRx/Metal measurement.

FIRST SLICE (P-P0-TILE-TVM-BENCH). This is a **measurement** harness over the
existing ``pcc.kernel_ir`` package, NOT a device benchmark and NOT a runtime.
It builds kernel IR for a fixed set of kernel shapes, freezes them through the
TIRx-compatible adapter, splits host/device via the TargetMachine registry, and
reports **logical compile-side metrics only**: IR node counts, host/device split
node counts, plain-TIR freeze success, and a TVM-oracle golden match.

Mode taxonomy (see :class:`TileBenchMode`):

* ``cpu-only``          -> RUNS. Everything measured here is a logical,
  compile-side surrogate: IR node counts, host/device split counts, plain-TIR
  freeze success, TVM-oracle golden match. No wall-clock, no TFLOPS, no launch.
* ``metal-source-only`` -> RUNS **iff** ``metal_finalize`` reports a descriptor
  (``STATUS_DESCRIPTOR_ONLY`` or ``STATUS_SKIPPED_WITH_REASON`` both carry a
  descriptor); else SKIPPED_WITH_REASON. Measures the emitted device-source
  descriptor metadata + packaging plan. NO ``.metallib``, NO device codegen.
* ``metal-runtime``     -> ALWAYS SKIPPED_WITH_REASON. No host launch is
  claimed here; launch-latency / TFLOPS are ``not-measured`` placeholders.

CLAIM BOUNDARY (measurement target only; see docs/design/pcc-tile-bench.md §3):
every "launch latency" / "TFLOPS" / "throughput" value produced here is the
literal placeholder :data:`NOT_MEASURED`. A speed claim requires BOTH IR-shape
evidence AND a real hardware run — neither exists in this slice. These counters
describe the *logical IR shape*, never a measured resource.

This module imports ``pcc.kernel_ir.*`` only; it does NOT touch ``pcc/__init__``.

    from tests.benchmarks.tile.harness import run_cpu_only_bench, TileBenchMode
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from pcc.kernel_ir.ir import (
    BufferParam,
    KernelFunc,
    KernelModule,
    KernelOp,
    Layout,
    LocalBuffer,
    MemoryScope,
    ScalarParam,
    ScalarType,
    validate_kernel,
)
from pcc.kernel_ir.metal_finalize import (
    STATUS_DESCRIPTOR_ONLY,
    STATUS_SKIPPED_WITH_REASON,
    finalize_metal,
)
from pcc.kernel_ir.target_split import (
    DeviceTarget,
    HostBackend,
    TargetMachine,
    resolve,
)
from pcc.kernel_ir.tirx_adapter import (
    PLAIN_TIR_FREEZE_MARKER,
    lower_to_plain_tir,
)
from pcc.kernel_ir.tvm_oracle import project_to_tir_shape

# The placeholder every not-measured resource metric MUST be. Never a number.
NOT_MEASURED = "not-measured"


class TileBenchMode(enum.Enum):
    """Measurement mode. See module docstring for the RUNS/SKIPPED contract."""

    CPU_ONLY = "cpu-only"
    METAL_SOURCE_ONLY = "metal-source-only"
    METAL_RUNTIME = "metal-runtime"


class RunStatus(enum.Enum):
    RUN = "RUN"
    SKIPPED_WITH_REASON = "SKIPPED_WITH_REASON"


# ---------------------------------------------------------------------------
# Kernel shape builders. Each returns a validated single-func KernelModule with
# a DETERMINISTIC node count. The counts below are the golden assertions the
# test pins; changing a builder changes the pinned count on purpose.
# ---------------------------------------------------------------------------

KERNEL_SHAPES: tuple[str, ...] = ("vector-add", "copy", "fill", "reduction", "gemm")


def _vector_add() -> KernelModule:
    """out[i] = a[i] + b[i], modeled as a parallel loop + copy-style tile ops."""
    func = KernelFunc(
        name="vector_add",
        params=(
            BufferParam("a", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
            BufferParam("b", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
            BufferParam("out", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
        ),
        body=(
            KernelOp("parallel", ("a", "b", "out"), {"extent": 1024}),
            KernelOp("copy", ("a", "out")),
        ),
        grid=(8,),
        threads=128,
    )
    return KernelModule("vector_add_mod", funcs=(func,))


def _copy() -> KernelModule:
    func = KernelFunc(
        name="copy_kernel",
        params=(
            BufferParam("src", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
            BufferParam("dst", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
        ),
        body=(KernelOp("copy", ("src", "dst")),),
        grid=(8,),
        threads=128,
    )
    return KernelModule("copy_mod", funcs=(func,))


def _fill() -> KernelModule:
    func = KernelFunc(
        name="fill_kernel",
        params=(BufferParam("dst", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),),
        body=(KernelOp("fill", ("dst",), {"value": 0}),),
        grid=(8,),
        threads=128,
    )
    return KernelModule("fill_mod", funcs=(func,))


def _reduction() -> KernelModule:
    func = KernelFunc(
        name="reduction_kernel",
        params=(
            BufferParam("src", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
            BufferParam("out", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
        ),
        locals=(
            LocalBuffer("acc", ScalarType.F32, shape=(128,), scope=MemoryScope.SHARED),
        ),
        body=(
            KernelOp("fill", ("acc",), {"value": 0}),
            KernelOp("reduce", ("src", "acc"), {"reduction": "sum"}),
            KernelOp("barrier", ()),
            KernelOp("copy", ("acc", "out")),
        ),
        grid=(8,),
        threads=128,
    )
    return KernelModule("reduction_mod", funcs=(func,))


def _gemm() -> KernelModule:
    """Tile GEMM mirroring the TileLang matmul_metal shape (A_shared/B_shared/
    C_local + clear + copy + gemm + copy-back), but as kernel IR node records."""
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam("A", ScalarType.F16, rank=2, scope=MemoryScope.GLOBAL, layout=Layout.ROW_MAJOR),
            BufferParam("B", ScalarType.F16, rank=2, scope=MemoryScope.GLOBAL, layout=Layout.ROW_MAJOR),
            BufferParam("C", ScalarType.F32, rank=2, scope=MemoryScope.GLOBAL, layout=Layout.ROW_MAJOR),
        ),
        locals=(
            LocalBuffer("A_shared", ScalarType.F16, shape=(16, 16), scope=MemoryScope.SHARED, layout=Layout.TILE),
            LocalBuffer("B_shared", ScalarType.F16, shape=(16, 16), scope=MemoryScope.SHARED, layout=Layout.TILE),
            LocalBuffer("C_local", ScalarType.F32, shape=(16, 16), scope=MemoryScope.FRAGMENT, layout=Layout.TILE),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared")),
            KernelOp("copy", ("B", "B_shared")),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local")),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(64, 64),
        threads=128,
    )
    return KernelModule("gemm_mod", funcs=(func,))


_BUILDERS = {
    "vector-add": _vector_add,
    "copy": _copy,
    "fill": _fill,
    "reduction": _reduction,
    "gemm": _gemm,
}


def build_kernel(shape: str) -> KernelModule:
    """Build + validate the kernel IR module for a named shape."""
    builder = _BUILDERS.get(shape)
    if builder is None:
        raise KeyError(
            f"unknown kernel shape {shape!r}; known: {sorted(_BUILDERS)}"
        )
    return validate_kernel(builder())


# ---------------------------------------------------------------------------
# Metric records. These are LOGICAL compile-side counters, never resources.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IrNodeCounts:
    """Deterministic node counts over the kernel IR module."""

    funcs: int
    params: int
    local_buffers: int
    body_ops: int
    scalar_params: int
    buffer_params: int

    def to_dict(self) -> dict[str, int]:
        return {
            "funcs": self.funcs,
            "params": self.params,
            "local_buffers": self.local_buffers,
            "body_ops": self.body_ops,
            "scalar_params": self.scalar_params,
            "buffer_params": self.buffer_params,
        }


@dataclass(frozen=True)
class HostDeviceSplit:
    """Node counts on each side of the host/device split.

    The shared front-half (target-neutral) is counted once; the device-side
    node count is the number of frozen plain-TIR ops that a device finalize
    would consume; the host-side node count is the launcher/stub surface (funcs
    that own the launch). This is a LOGICAL split, from ``target_split`` +
    frozen plain TIR, never a measured partition of machine code.
    """

    shared_front_half_nodes: int
    host_side_nodes: int
    device_side_nodes: int
    runs_device_finalize: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "shared_front_half_nodes": self.shared_front_half_nodes,
            "host_side_nodes": self.host_side_nodes,
            "device_side_nodes": self.device_side_nodes,
            "runs_device_finalize": self.runs_device_finalize,
        }


@dataclass(frozen=True)
class ResourcePlaceholders:
    """The resource metrics this slice does NOT measure. All are NOT_MEASURED.

    Present so a reader sees exactly which numbers are absent and why — not to
    imply they were attempted. A real value requires hardware + IR-shape proof.
    """

    launch_latency_ms: str = NOT_MEASURED
    tflops: str = NOT_MEASURED
    device_throughput_gbps: str = NOT_MEASURED

    def to_dict(self) -> dict[str, str]:
        return {
            "launch_latency_ms": self.launch_latency_ms,
            "tflops": self.tflops,
            "device_throughput_gbps": self.device_throughput_gbps,
        }


@dataclass(frozen=True)
class CpuOnlyKernelReport:
    """cpu-only measurement result for ONE kernel shape."""

    shape: str
    ir_node_counts: IrNodeCounts
    host_device_split: HostDeviceSplit
    plain_tir_freeze_ok: bool
    tvm_oracle_golden_match: bool
    resources: ResourcePlaceholders = field(default_factory=ResourcePlaceholders)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "ir_node_counts": self.ir_node_counts.to_dict(),
            "host_device_split": self.host_device_split.to_dict(),
            "plain_tir_freeze_ok": self.plain_tir_freeze_ok,
            "tvm_oracle_golden_match": self.tvm_oracle_golden_match,
            "resources_not_measured": self.resources.to_dict(),
        }


@dataclass(frozen=True)
class MetalSourceOnlyReport:
    """metal-source-only measurement result for ONE kernel shape."""

    shape: str
    status: RunStatus
    reason: str | None
    library_name: str | None
    entry_points: list[str]
    packaging_steps: list[str]
    metallib_produced: bool
    host_launch_claimed: bool
    resources: ResourcePlaceholders = field(default_factory=ResourcePlaceholders)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "status": self.status.value,
            "reason": self.reason,
            "library_name": self.library_name,
            "entry_points": list(self.entry_points),
            "packaging_steps": list(self.packaging_steps),
            "metallib_produced": self.metallib_produced,
            "host_launch_claimed": self.host_launch_claimed,
            "resources_not_measured": self.resources.to_dict(),
        }


@dataclass(frozen=True)
class MetalRuntimeReport:
    """metal-runtime result — ALWAYS skipped; never a launch claim."""

    shape: str
    status: RunStatus
    reason: str
    resources: ResourcePlaceholders = field(default_factory=ResourcePlaceholders)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "status": self.status.value,
            "reason": self.reason,
            "resources_not_measured": self.resources.to_dict(),
        }


# ---------------------------------------------------------------------------
# Mode availability probes.
# ---------------------------------------------------------------------------


def metal_toolchain_available() -> bool:
    """Mirror of metal_finalize's real probe: Metal compiler can execute."""
    from pcc.backend.metal import metal_toolchain_usable

    return metal_toolchain_usable()


def _count_ir_nodes(module: KernelModule) -> IrNodeCounts:
    funcs = len(module.funcs)
    params = sum(len(f.params) for f in module.funcs)
    local_buffers = sum(len(f.locals) for f in module.funcs)
    body_ops = sum(len(f.body) for f in module.funcs)
    scalar_params = sum(
        1 for f in module.funcs for p in f.params if isinstance(p, ScalarParam)
    )
    buffer_params = sum(
        1 for f in module.funcs for p in f.params if isinstance(p, BufferParam)
    )
    return IrNodeCounts(
        funcs=funcs,
        params=params,
        local_buffers=local_buffers,
        body_ops=body_ops,
        scalar_params=scalar_params,
        buffer_params=buffer_params,
    )


def _measure_host_device_split(module: KernelModule) -> HostDeviceSplit:
    """Resolve host=self + device=metal, freeze, and count the split.

    host=self is used deliberately: the harness measures the first-class
    self-backend split, and ``resolve`` guarantees NO silent LLVM fallback.
    """
    machine: TargetMachine = resolve(host=HostBackend.SELF, device=DeviceTarget.METAL)
    plain = lower_to_plain_tir(module, target="metal")
    # Device-side nodes = frozen plain-TIR ops (what a device finalize consumes).
    device_side = sum(len(f["ops"]) for f in plain.funcs)
    # Host-side nodes = one launcher stub per kernel func (the launch surface).
    host_side = len(plain.funcs)
    return HostDeviceSplit(
        shared_front_half_nodes=len(machine.shared_front_half),
        host_side_nodes=host_side,
        device_side_nodes=device_side,
        runs_device_finalize=machine.runs_device_finalize,
    )


def _plain_tir_freeze_ok(module: KernelModule) -> bool:
    plain = lower_to_plain_tir(module, target="metal")
    return plain.marker == PLAIN_TIR_FREEZE_MARKER


def _oracle_golden_match(module: KernelModule) -> bool:
    """The TVM-oracle golden match: the projection must equal itself (a stable,
    round-trippable shape) and every func must project to a well-formed
    PrimFunc-shaped object with params + buffer_map + body."""
    shape = project_to_tir_shape(module)
    if project_to_tir_shape(module) != shape:  # determinism
        return False
    if shape["tir_object_shape_version"] != 1:
        return False
    for fn in shape["functions"]:
        if not {"prim_func", "params", "buffer_map", "body"} <= set(fn.keys()):
            return False
    return True


# ---------------------------------------------------------------------------
# The three mode runners.
# ---------------------------------------------------------------------------


def run_cpu_only_bench(shape: str) -> CpuOnlyKernelReport:
    """cpu-only mode: RUNS. Measure logical compile-side metrics for *shape*."""
    module = build_kernel(shape)
    return CpuOnlyKernelReport(
        shape=shape,
        ir_node_counts=_count_ir_nodes(module),
        host_device_split=_measure_host_device_split(module),
        plain_tir_freeze_ok=_plain_tir_freeze_ok(module),
        tvm_oracle_golden_match=_oracle_golden_match(module),
    )


def run_metal_source_only(shape: str) -> MetalSourceOnlyReport:
    """metal-source-only mode: RUNS iff a descriptor is reported, else SKIPPED.

    Never produces a ``.metallib`` and never claims a host launch. The descriptor
    is produced whether or not the toolchain is present (finalize_metal always
    attaches a descriptor); the RUN/SKIP split is on toolchain availability.
    """
    module = build_kernel(shape)
    result = finalize_metal(module)  # real probe; descriptor attached either way
    desc = result.descriptor

    if desc is None:
        return MetalSourceOnlyReport(
            shape=shape,
            status=RunStatus.SKIPPED_WITH_REASON,
            reason=(
                "metal_finalize reported no packaging descriptor; nothing to "
                "measure. No .metallib produced, no host launch claimed."
            ),
            library_name=None,
            entry_points=[],
            packaging_steps=[],
            metallib_produced=False,
            host_launch_claimed=False,
        )

    steps = [s["step"] for s in desc.steps]
    if result.status == STATUS_SKIPPED_WITH_REASON:
        # Toolchain absent: we still measured the DESCRIPTOR metadata (the point
        # of metal-source-only), but flag it as a skip-with-reason for honesty.
        status = RunStatus.SKIPPED_WITH_REASON
    else:
        # STATUS_DESCRIPTOR_ONLY: toolchain present but no source emission yet.
        status = RunStatus.RUN

    return MetalSourceOnlyReport(
        shape=shape,
        status=status,
        reason=result.reason,
        library_name=desc.library_name,
        entry_points=list(desc.entry_points),
        packaging_steps=steps,
        metallib_produced=False,
        host_launch_claimed=False,
    )


def run_metal_runtime(shape: str) -> MetalRuntimeReport:
    """metal-runtime mode: ALWAYS SKIPPED_WITH_REASON. No launch is claimed."""
    # Touch the builder so a shape typo still fails loudly rather than silently
    # claiming a skip for a nonexistent kernel.
    build_kernel(shape)
    return MetalRuntimeReport(
        shape=shape,
        status=RunStatus.SKIPPED_WITH_REASON,
        reason=(
            "metal-runtime is never exercised in this slice: no host launch, no "
            "GPU execution, no .metallib. launch-latency / TFLOPS remain "
            f"{NOT_MEASURED!r}. A speed claim requires hardware + IR-shape proof."
        ),
    )


def run_all_modes(shape: str) -> dict[str, Any]:
    """Convenience: run every mode for one shape and collect the reports."""
    return {
        TileBenchMode.CPU_ONLY.value: run_cpu_only_bench(shape).to_dict(),
        TileBenchMode.METAL_SOURCE_ONLY.value: run_metal_source_only(shape).to_dict(),
        TileBenchMode.METAL_RUNTIME.value: run_metal_runtime(shape).to_dict(),
    }


__all__ = [
    "NOT_MEASURED",
    "TileBenchMode",
    "RunStatus",
    "KERNEL_SHAPES",
    "build_kernel",
    "IrNodeCounts",
    "HostDeviceSplit",
    "ResourcePlaceholders",
    "CpuOnlyKernelReport",
    "MetalSourceOnlyReport",
    "MetalRuntimeReport",
    "metal_toolchain_available",
    "run_cpu_only_bench",
    "run_metal_source_only",
    "run_metal_runtime",
    "run_all_modes",
]
