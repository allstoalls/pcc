"""Metal launch planning for Kernel IR.

This module is the next boundary after ``host_device_split`` and
``metal_finalize``: it validates the runtime launch packet and records the
Metal command-encoder shape a real runtime must execute.

It still does not execute a GPU command buffer. If execution is requested before
a Kernel-IR Metal runtime exists, the result is ``SKIPPED_WITH_REASON`` rather
than a fake launch claim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcc.kernel_ir.hmm_fence import (
    BufferState,
    HmmFenceError,
    PccBufferHandle,
    PccPackedArgs,
)
from pcc.kernel_ir.host_device_split import (
    HostDeviceSplitProof,
    KernelArgBinding,
    KernelLaunchBoundary,
    build_host_launch_boundaries,
)
from pcc.kernel_ir.ir import KernelModule
from pcc.kernel_ir.metal_finalize import metal_device_entry_name
from pcc.kernel_ir.tirx_adapter import PlainTirModule, lower_to_plain_tir

STATUS_PLAN_ONLY = "metal_launch_plan_only"
STATUS_BRIDGE_SOURCE_ONLY = "metal_executor_bridge_source_only"
STATUS_BRIDGE_OBJECT_PRODUCED = "metal_executor_bridge_object_produced"
STATUS_SKIPPED_WITH_REASON = "SKIPPED_WITH_REASON"

_DTYPE_NBYTES = {
    "bool": 1,
    "i8": 1,
    "u8": 1,
    "i16": 2,
    "u16": 2,
    "i32": 4,
    "u32": 4,
    "i64": 8,
    "u64": 8,
    "f16": 2,
    "f32": 4,
    "f64": 8,
}

_SCALAR_C_TYPES = {
    "bool": "bool",
    "i8": "int8_t",
    "u8": "uint8_t",
    "i16": "int16_t",
    "u16": "uint16_t",
    "i32": "int32_t",
    "u32": "uint32_t",
    "i64": "int64_t",
    "u64": "uint64_t",
    "f16": "uint16_t",
    "f32": "float",
    "f64": "double",
}


class MetalLaunchError(ValueError):
    """A Metal launch packet or plan violates the Kernel IR launch contract."""


@dataclass(frozen=True)
class MetalDispatchShape:
    """The Metal dispatch dimensions for one Kernel IR launch."""

    api: str
    threadgroups_per_grid: tuple[int, int, int]
    threads_per_threadgroup: tuple[int, int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "api": self.api,
            "threadgroups_per_grid": list(self.threadgroups_per_grid),
            "threads_per_threadgroup": list(self.threads_per_threadgroup),
        }


@dataclass(frozen=True)
class MetalRuntimeArg:
    """One validated runtime argument binding for a Metal launch."""

    name: str
    kind: str
    dtype: str
    index: int
    address_space: str
    source: str
    shape: tuple[int, ...] | None = None
    required_nbytes: int | None = None
    handle_id: int | None = None
    provided_nbytes: int | None = None
    scalar_value: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "dtype": self.dtype,
            "index": self.index,
            "address_space": self.address_space,
            "source": self.source,
        }
        if self.shape is not None:
            data["shape"] = list(self.shape)
        if self.required_nbytes is not None:
            data["required_nbytes"] = self.required_nbytes
        if self.handle_id is not None:
            data["handle_id"] = self.handle_id
        if self.provided_nbytes is not None:
            data["provided_nbytes"] = self.provided_nbytes
        if self.scalar_value is not None:
            data["scalar_value"] = self.scalar_value
        return data


@dataclass(frozen=True)
class MetalLaunchPlan:
    """A validated, non-executed Metal launch plan."""

    kernel_entry: str
    launcher_symbol: str
    metallib_path: str | None
    metallib_available: bool
    dispatch: MetalDispatchShape
    args: tuple[MetalRuntimeArg, ...]
    command_encoder_steps: tuple[dict[str, Any], ...]
    host_device_split: HostDeviceSplitProof
    runtime_launch_executed: bool = False
    fence_required_on_commit: bool = True
    whole_program_gpu: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_mode": "Metal launch plan, not executed",
            "kernel_entry": self.kernel_entry,
            "launcher_symbol": self.launcher_symbol,
            "metallib_path": self.metallib_path,
            "metallib_available": self.metallib_available,
            "dispatch": self.dispatch.to_dict(),
            "args": [arg.to_dict() for arg in self.args],
            "command_encoder_steps": list(self.command_encoder_steps),
            "runtime_launch_executed": self.runtime_launch_executed,
            "fence_required_on_commit": self.fence_required_on_commit,
            "whole_program_gpu": self.whole_program_gpu,
            "host_device_split": self.host_device_split.to_dict(),
        }


@dataclass(frozen=True)
class MetalLaunchResult:
    """Result of preparing or attempting a Kernel-IR Metal launch."""

    status: str
    plan: MetalLaunchPlan
    reason: str
    runtime_launch_executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "runtime_launch_executed": self.runtime_launch_executed,
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True)
class MetalExecutorBridgeSource:
    """Source-only Objective-C bridge generated from a launch plan."""

    status: str
    source_name: str
    source: str
    plan: MetalLaunchPlan
    artifact_path: str | None = None
    runtime_launch_executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_name": self.source_name,
            "artifact_path": self.artifact_path,
            "source_produced": True,
            "runtime_launch_executed": self.runtime_launch_executed,
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True)
class MetalExecutorBridgeArtifacts:
    """Artifact build result for the Objective-C Metal executor bridge."""

    status: str
    source_name: str
    source: str
    plan: MetalLaunchPlan
    source_path: str
    object_path: str | None = None
    bridge_object_produced: bool = False
    reason: str = ""
    runtime_launch_executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "source_name": self.source_name,
            "source_path": self.source_path,
            "object_path": self.object_path,
            "source_produced": True,
            "bridge_object_produced": self.bridge_object_produced,
            "runtime_launch_executed": self.runtime_launch_executed,
            "plan": self.plan.to_dict(),
        }


def _coerce_plain(module: KernelModule | PlainTirModule) -> PlainTirModule:
    return module if isinstance(module, PlainTirModule) else lower_to_plain_tir(module, target="metal")


def _select_launch(boundary: HostDeviceSplitProof, entry: str | None) -> KernelLaunchBoundary:
    launches = boundary.launches
    if entry is None:
        if len(launches) != 1:
            names = [launch.kernel_entry for launch in launches]
            raise MetalLaunchError(f"multiple kernel launches {names}; choose entry=")
        return launches[0]
    for launch in launches:
        if launch.kernel_entry == entry or metal_device_entry_name(launch.kernel_entry) == entry:
            return launch
    names = [launch.kernel_entry for launch in launches]
    raise MetalLaunchError(f"kernel entry {entry!r} not found; available {names}")


def _select_func(plain: PlainTirModule, entry: str) -> dict[str, Any]:
    for func in plain.funcs:
        if func.get("name") == entry:
            return func
    raise MetalLaunchError(f"plain-TIR function {entry!r} not found")


def _dispatch_shape(func: dict[str, Any]) -> MetalDispatchShape:
    grid = func.get("grid")
    threads = func.get("threads")
    if not isinstance(grid, list) or not grid:
        raise MetalLaunchError("Metal launch requires a non-empty kernel grid")
    if len(grid) > 3 or any((not isinstance(dim, int)) or dim <= 0 for dim in grid):
        raise MetalLaunchError(f"bad Metal grid {grid!r}")
    if not isinstance(threads, int) or threads <= 0:
        raise MetalLaunchError(f"bad threads per threadgroup {threads!r}")
    padded_grid = tuple((grid + [1, 1, 1])[:3])
    return MetalDispatchShape(
        api="dispatchThreadgroups",
        threadgroups_per_grid=padded_grid,  # type: ignore[arg-type]
        threads_per_threadgroup=(threads, 1, 1),
    )


def _param_records(func: dict[str, Any]) -> dict[str, dict[str, Any]]:
    params = func.get("params")
    if not isinstance(params, list):
        raise MetalLaunchError("plain-TIR function has no params list")
    out: dict[str, dict[str, Any]] = {}
    for param in params:
        if not isinstance(param, dict):
            raise MetalLaunchError(f"bad parameter record {param!r}")
        name = param.get("name")
        if not isinstance(name, str):
            raise MetalLaunchError(f"bad parameter record {param!r}")
        out[name] = param
    return out


def _shape_and_required_nbytes(param: dict[str, Any]) -> tuple[tuple[int, ...] | None, int | None]:
    shape = param.get("shape")
    dtype = param.get("dtype")
    if shape is None:
        return None, None
    if not isinstance(dtype, str) or dtype not in _DTYPE_NBYTES:
        raise MetalLaunchError(f"cannot compute nbytes for dtype {dtype!r}")
    if not isinstance(shape, list) or not shape:
        raise MetalLaunchError(f"bad static buffer shape {shape!r}")
    count = 1
    dims: list[int] = []
    for dim in shape:
        if not isinstance(dim, int) or dim <= 0:
            raise MetalLaunchError(f"bad static buffer shape {shape!r}")
        dims.append(dim)
        count *= dim
    return tuple(dims), count * _DTYPE_NBYTES[dtype]


def _validate_launch_device(packed_args: PccPackedArgs) -> None:
    if not packed_args.launch_device.startswith("metal"):
        raise MetalLaunchError(
            f"Metal launch requires packed_args.launch_device='metal:*', "
            f"got {packed_args.launch_device!r}"
        )


def _buffer_arg(
    *,
    binding: KernelArgBinding,
    param: dict[str, Any],
    handle: PccBufferHandle,
) -> MetalRuntimeArg:
    if handle.state is not BufferState.LIVE:
        raise MetalLaunchError(
            f"buffer {handle.handle_id} for {binding.name!r} is {handle.state.value}, "
            "not live"
        )
    if handle.dtype != binding.dtype:
        raise MetalLaunchError(
            f"buffer {binding.name!r} expects dtype {binding.dtype!r}, "
            f"got handle dtype {handle.dtype!r}"
        )
    shape, required_nbytes = _shape_and_required_nbytes(param)
    if required_nbytes is not None and handle.nbytes < required_nbytes:
        raise MetalLaunchError(
            f"buffer {binding.name!r} requires at least {required_nbytes} bytes "
            f"from shape metadata, got {handle.nbytes}"
        )
    return MetalRuntimeArg(
        name=binding.name,
        kind="buffer",
        dtype=binding.dtype,
        index=binding.index,
        address_space=binding.address_space,
        source="PccBufferHandle",
        shape=shape,
        required_nbytes=required_nbytes,
        handle_id=handle.handle_id,
        provided_nbytes=handle.nbytes,
    )


def _scalar_arg(
    *,
    binding: KernelArgBinding,
    scalar: tuple[str, Any],
) -> MetalRuntimeArg:
    dtype, value = scalar
    if dtype != binding.dtype:
        raise MetalLaunchError(
            f"scalar {binding.name!r} expects dtype {binding.dtype!r}, got {dtype!r}"
        )
    return MetalRuntimeArg(
        name=binding.name,
        kind="scalar",
        dtype=binding.dtype,
        index=binding.index,
        address_space=binding.address_space,
        source="POD scalar",
        scalar_value=value,
    )


def _runtime_args(
    launch: KernelLaunchBoundary,
    func: dict[str, Any],
    packed_args: PccPackedArgs,
) -> tuple[MetalRuntimeArg, ...]:
    _validate_launch_device(packed_args)
    try:
        packed_args.validate()
    except HmmFenceError:
        raise
    params = _param_records(func)
    buffer_index = 0
    scalar_index = 0
    args: list[MetalRuntimeArg] = []
    for binding in launch.arg_bindings:
        if binding.kind == "buffer":
            if buffer_index >= len(packed_args.buffers):
                raise MetalLaunchError(f"missing buffer argument for {binding.name!r}")
            param = params.get(binding.name)
            if param is None:
                raise MetalLaunchError(f"missing parameter metadata for {binding.name!r}")
            args.append(
                _buffer_arg(
                    binding=binding,
                    param=param,
                    handle=packed_args.buffers[buffer_index],
                )
            )
            buffer_index += 1
        elif binding.kind == "scalar":
            if scalar_index >= len(packed_args.scalars):
                raise MetalLaunchError(f"missing scalar argument for {binding.name!r}")
            args.append(
                _scalar_arg(
                    binding=binding,
                    scalar=packed_args.scalars[scalar_index],
                )
            )
            scalar_index += 1
        else:
            raise MetalLaunchError(f"unsupported runtime binding kind {binding.kind!r}")
    if buffer_index != len(packed_args.buffers):
        raise MetalLaunchError("extra buffer arguments supplied to Metal launch")
    if scalar_index != len(packed_args.scalars):
        raise MetalLaunchError("extra scalar arguments supplied to Metal launch")
    return tuple(args)


def _metallib_available(path: str | Path | None) -> bool:
    return path is not None and Path(path).is_file()


def _command_steps(plan_args: tuple[MetalRuntimeArg, ...], dispatch: MetalDispatchShape) -> tuple[dict[str, Any], ...]:
    steps: list[dict[str, Any]] = [
        {"step": "load_metallib"},
        {"step": "newFunctionWithName"},
        {"step": "newComputePipelineState"},
        {"step": "newCommandQueue"},
        {"step": "commandBuffer"},
        {"step": "computeCommandEncoder"},
        {"step": "setComputePipelineState"},
    ]
    for arg in plan_args:
        if arg.kind == "buffer":
            steps.append(
                {
                    "step": "setBuffer",
                    "name": arg.name,
                    "index": arg.index,
                    "handle_id": arg.handle_id,
                }
            )
        elif arg.kind == "scalar":
            steps.append(
                {
                    "step": "setBytes",
                    "name": arg.name,
                    "index": arg.index,
                    "dtype": arg.dtype,
                }
            )
    steps.extend(
        [
            {"step": dispatch.api, "dispatch": dispatch.to_dict()},
            {"step": "endEncoding"},
            {"step": "commit"},
            {"step": "complete_fence_on_command_buffer_completion"},
        ]
    )
    return tuple(steps)


def _c_identifier(name: str) -> str:
    chars: list[str] = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            chars.append(ch)
        else:
            chars.append("_")
    ident = "".join(chars) or "pcc_metal_kernel"
    if ident[0].isdigit():
        ident = "_" + ident
    return ident


def _objc_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _u3(values: tuple[int, int, int]) -> tuple[str, str, str]:
    return tuple(f"{value}u" for value in values)  # type: ignore[return-value]


def metal_executor_bridge_symbol(
    plan: MetalLaunchPlan,
    *,
    function_name: str | None = None,
) -> str:
    """Return the C symbol emitted for a plan's Objective-C bridge."""
    return _c_identifier(function_name or f"{plan.launcher_symbol}_runtime_bridge")


def emit_metal_executor_bridge_source(
    plan: MetalLaunchPlan,
    *,
    function_name: str | None = None,
) -> str:
    """Emit Objective-C source for a future Kernel-IR Metal executor bridge.

    The generated source is intentionally source-only. It describes the concrete
    Metal calls implied by ``plan`` but is not compiled or executed here.
    """
    symbol = metal_executor_bridge_symbol(plan, function_name=function_name)
    kernel = _objc_string(plan.kernel_entry)
    tgx, tgy, tgz = _u3(plan.dispatch.threadgroups_per_grid)
    thx, thy, thz = _u3(plan.dispatch.threads_per_threadgroup)
    buffer_args = [arg for arg in plan.args if arg.kind == "buffer"]
    scalar_args = [arg for arg in plan.args if arg.kind == "scalar"]

    lines: list[str] = [
        "#import <Foundation/Foundation.h>",
        "#import <Metal/Metal.h>",
        "#include <stdint.h>",
        "#include <stdbool.h>",
        "",
        "typedef void (*pcc_metal_fence_complete_fn)(void *ctx);",
        "",
        f"int64_t {symbol}(",
        "    const char *metallib_path,",
        "    void *const *buffer_handles,",
        "    const void *const *scalar_values,",
        "    pcc_metal_fence_complete_fn fence_complete,",
        "    void *fence_ctx,",
        "    bool wait_until_completed)",
        "{",
        "  @autoreleasepool {",
        "    id<MTLDevice> device = MTLCreateSystemDefaultDevice();",
        "    if (device == nil) { return 3; }",
        "    if (metallib_path == NULL) { return 2; }",
        "    NSError *error = nil;",
        "    NSString *path = [NSString stringWithUTF8String:metallib_path];",
        "    NSURL *url = [NSURL fileURLWithPath:path];",
        "    id<MTLLibrary> library = [device newLibraryWithURL:url error:&error];",
        "    if (library == nil) { return 4; }",
        f"    id<MTLFunction> function = [library newFunctionWithName:@\"{kernel}\"];",
        "    if (function == nil) { return 5; }",
        "    id<MTLComputePipelineState> pipeline =",
        "        [device newComputePipelineStateWithFunction:function error:&error];",
        "    if (pipeline == nil) { return 6; }",
        "    id<MTLCommandQueue> queue = [device newCommandQueue];",
        "    if (queue == nil) { return 7; }",
        "    id<MTLCommandBuffer> command_buffer = [queue commandBuffer];",
        "    id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];",
        "    if (encoder == nil) { return 8; }",
        "    [encoder setComputePipelineState:pipeline];",
    ]

    for ordinal, arg in enumerate(buffer_args):
        lines.extend(
            [
                f"    if (buffer_handles == NULL || buffer_handles[{ordinal}] == NULL) {{ return 10; }}",
                f"    id<MTLBuffer> pcc_buf_{_c_identifier(arg.name)} = "
                f"(__bridge id<MTLBuffer>)buffer_handles[{ordinal}];",
                f"    [encoder setBuffer:pcc_buf_{_c_identifier(arg.name)} offset:0 atIndex:{arg.index}];",
            ]
        )

    for ordinal, arg in enumerate(scalar_args):
        ctype = _SCALAR_C_TYPES.get(arg.dtype)
        if ctype is None:
            raise MetalLaunchError(f"no bridge scalar type for dtype {arg.dtype!r}")
        lines.extend(
            [
                f"    if (scalar_values == NULL || scalar_values[{ordinal}] == NULL) {{ return 11; }}",
                f"    const {ctype} *pcc_scalar_{_c_identifier(arg.name)} = "
                f"(const {ctype} *)scalar_values[{ordinal}];",
                f"    [encoder setBytes:pcc_scalar_{_c_identifier(arg.name)} "
                f"length:sizeof({ctype}) atIndex:{arg.index}];",
            ]
        )

    lines.extend(
        [
            f"    MTLSize grid = MTLSizeMake({tgx}, {tgy}, {tgz});",
            f"    MTLSize threads = MTLSizeMake({thx}, {thy}, {thz});",
            "    [encoder dispatchThreadgroups:grid threadsPerThreadgroup:threads];",
            "    [encoder endEncoding];",
            "    if (fence_complete != NULL) {",
            "      [command_buffer addCompletedHandler:^(id<MTLCommandBuffer> cb) {",
            "        (void)cb;",
            "        fence_complete(fence_ctx);",
            "      }];",
            "    }",
            "    [command_buffer commit];",
            "    if (wait_until_completed) {",
            "      [command_buffer waitUntilCompleted];",
            "      if ([command_buffer error] != nil) { return 12; }",
            "    }",
            "    return 0;",
            "  }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def write_metal_executor_bridge_source(
    plan: MetalLaunchPlan,
    artifact_dir: str | Path,
    *,
    function_name: str | None = None,
) -> MetalExecutorBridgeSource:
    """Write the source-only executor bridge for *plan* into *artifact_dir*."""
    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_name = f"{_c_identifier(plan.kernel_entry)}_metal_bridge.m"
    source = emit_metal_executor_bridge_source(plan, function_name=function_name)
    path = out_dir / source_name
    path.write_text(source, encoding="utf-8")
    return MetalExecutorBridgeSource(
        status=STATUS_BRIDGE_SOURCE_ONLY,
        source_name=source_name,
        source=source,
        plan=plan,
        artifact_path=str(path),
    )


def build_metal_executor_bridge_artifacts(
    plan: MetalLaunchPlan,
    artifact_dir: str | Path,
    *,
    function_name: str | None = None,
    compile_bridge: bool = False,
    compiler: Callable[..., Path] | None = None,
    timeout: float = 30.0,
) -> MetalExecutorBridgeArtifacts:
    """Write bridge source and optionally compile it to an object artifact.

    This is still an artifact-production boundary, not runtime execution. A
    missing Objective-C/Metal toolchain is reported as ``SKIPPED_WITH_REASON``
    after the source artifact is written. A compiler rejection of the generated
    bridge source is treated as a launch-layer error so invalid source is not
    hidden as an environmental skip.
    """
    source_artifact = write_metal_executor_bridge_source(
        plan,
        artifact_dir,
        function_name=function_name,
    )
    if source_artifact.artifact_path is None:
        raise MetalLaunchError("executor bridge source writer did not return a path")
    source_path = Path(source_artifact.artifact_path)
    object_path = source_path.with_suffix(".o")

    if not compile_bridge:
        return MetalExecutorBridgeArtifacts(
            status=STATUS_BRIDGE_SOURCE_ONLY,
            source_name=source_artifact.source_name,
            source=source_artifact.source,
            plan=plan,
            source_path=str(source_path),
            reason=(
                "Executor bridge source artifact written; compile_bridge=False, "
                "so no object was produced and no command buffer was committed."
            ),
        )

    if compiler is None:
        from pcc.backend.metal import compile_metal_runtime_bridge

        compiler = compile_metal_runtime_bridge

    from pcc.backend.metal import MetalCompileError, MetalToolchainUnavailable

    try:
        compiled_path = compiler(
            object_path,
            source_path=source_path,
            timeout=timeout,
        )
    except MetalToolchainUnavailable as exc:
        return MetalExecutorBridgeArtifacts(
            status=STATUS_SKIPPED_WITH_REASON,
            source_name=source_artifact.source_name,
            source=source_artifact.source,
            plan=plan,
            source_path=str(source_path),
            object_path=str(object_path),
            reason=(
                "Metal executor bridge compiler unavailable; source artifact "
                f"was written but no object was produced: {exc}"
            ),
        )
    except MetalCompileError as exc:
        raise MetalLaunchError(
            f"Metal executor bridge source failed to compile: {exc}"
        ) from exc

    compiled = Path(compiled_path)
    if not compiled.is_file():
        raise MetalLaunchError(
            f"Metal executor bridge compiler returned no object artifact: {compiled}"
        )
    return MetalExecutorBridgeArtifacts(
        status=STATUS_BRIDGE_OBJECT_PRODUCED,
        source_name=source_artifact.source_name,
        source=source_artifact.source,
        plan=plan,
        source_path=str(source_path),
        object_path=str(compiled),
        bridge_object_produced=True,
        reason=(
            "Executor bridge source artifact written and Objective-C object "
            "artifact produced; no command buffer was committed."
        ),
    )


def plan_metal_launch(
    module: KernelModule | PlainTirModule,
    packed_args: PccPackedArgs,
    *,
    metallib_path: str | Path | None = None,
    entry: str | None = None,
) -> MetalLaunchPlan:
    """Validate and build a non-executed Metal launch plan."""
    plain = _coerce_plain(module)
    boundary = build_host_launch_boundaries(plain, host="self", device="metal")
    launch = _select_launch(boundary, entry)
    func = _select_func(plain, launch.kernel_entry)
    dispatch = _dispatch_shape(func)
    args = _runtime_args(launch, func, packed_args)
    return MetalLaunchPlan(
        kernel_entry=metal_device_entry_name(launch.kernel_entry),
        launcher_symbol=launch.launcher_symbol,
        metallib_path=str(metallib_path) if metallib_path is not None else None,
        metallib_available=_metallib_available(metallib_path),
        dispatch=dispatch,
        args=args,
        command_encoder_steps=_command_steps(args, dispatch),
        host_device_split=boundary,
    )


def prepare_metal_launch(
    module: KernelModule | PlainTirModule,
    packed_args: PccPackedArgs,
    *,
    metallib_path: str | Path | None = None,
    entry: str | None = None,
    execute: bool = False,
) -> MetalLaunchResult:
    """Prepare a Kernel-IR Metal launch.

    ``execute=False`` is a pure validation/planning path. ``execute=True``
    currently returns ``SKIPPED_WITH_REASON`` after validation because Kernel IR
    has no command-buffer executor yet.
    """
    plan = plan_metal_launch(
        module,
        packed_args,
        metallib_path=metallib_path,
        entry=entry,
    )
    if not execute:
        return MetalLaunchResult(
            status=STATUS_PLAN_ONLY,
            plan=plan,
            reason=(
                "Metal launch packet validated and command encoder plan built; "
                "execute=False, so no command buffer was committed and no fence "
                "was completed."
            ),
        )
    if not plan.metallib_available:
        return MetalLaunchResult(
            status=STATUS_SKIPPED_WITH_REASON,
            plan=plan,
            reason=(
                "Metal launch execution requested, but no usable metallib path "
                "was supplied. No command buffer was committed."
            ),
        )
    return MetalLaunchResult(
        status=STATUS_SKIPPED_WITH_REASON,
        plan=plan,
        reason=(
            "Metal launch execution requested and metallib exists, but the "
            "Kernel IR command-buffer executor is not implemented yet. No "
            "command buffer was committed."
        ),
    )


__all__ = [
    "STATUS_PLAN_ONLY",
    "STATUS_BRIDGE_SOURCE_ONLY",
    "STATUS_BRIDGE_OBJECT_PRODUCED",
    "STATUS_SKIPPED_WITH_REASON",
    "MetalLaunchError",
    "MetalDispatchShape",
    "MetalRuntimeArg",
    "MetalLaunchPlan",
    "MetalLaunchResult",
    "MetalExecutorBridgeSource",
    "MetalExecutorBridgeArtifacts",
    "build_metal_executor_bridge_artifacts",
    "emit_metal_executor_bridge_source",
    "metal_executor_bridge_symbol",
    "plan_metal_launch",
    "prepare_metal_launch",
    "write_metal_executor_bridge_source",
]
