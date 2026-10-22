"""Runtime-source Metal bridge for Kernel IR launches.

This module is deliberately separate from the ``.metallib`` package path. It
uses Metal's ``newLibraryWithSource`` runtime compiler so a local machine can
exercise command-buffer submission even when the offline ``metal`` toolchain is
missing. A successful call proves a runtime-source launch boundary, not a
produced metallib artifact.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcc.kernel_ir.hmm_fence import PccFenceToken
from pcc.kernel_ir.metal_buffer import (
    MetalNativeBufferAllocationSet,
    MetalNativeBufferBindingSet,
    MetalNativeBufferRuntimeArtifacts,
)
from pcc.kernel_ir.metal_finalize import MetalFinalizeResult
from pcc.kernel_ir.metal_launch import MetalLaunchPlan
from pcc.kernel_ir.metal_runtime_abi import build_metal_source_runtime_call_plan
from pcc.kernel_ir.metal_tensor import MetalMatrixTransferSet
from pcc.kernel_ir.metal_verify import MetalCpuOracleComparisonResult

STATUS_SOURCE_RUNTIME_BRIDGE_SOURCE_ONLY = "metal_source_runtime_bridge_source_only"
STATUS_SOURCE_RUNTIME_BRIDGE_OBJECT_PRODUCED = "metal_source_runtime_bridge_object_produced"
STATUS_SOURCE_RUNTIME_BRIDGE_LIBRARY_PRODUCED = "metal_source_runtime_bridge_library_produced"
STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED = "metal_source_runtime_bridge_load_validated"
STATUS_SOURCE_RUNTIME_INVOCATION_ABI_VALIDATED = "metal_source_runtime_invocation_abi_validated"
STATUS_SOURCE_RUNTIME_INVOKED = "metal_source_runtime_invoked"
STATUS_SOURCE_RUNTIME_INVOCATION_FAILED = "metal_source_runtime_invocation_failed"
STATUS_SOURCE_RUNTIME_PACKAGE_EXECUTED = "metal_source_runtime_package_executed"
STATUS_SOURCE_RUNTIME_PACKAGE_ABI_VALIDATED = "metal_source_runtime_package_abi_validated"
STATUS_SOURCE_RUNTIME_PACKAGE_FAILED = "metal_source_runtime_package_failed"
STATUS_SKIPPED_WITH_REASON = "SKIPPED_WITH_REASON"


class MetalSourceRuntimeError(ValueError):
    """A runtime-source Metal bridge or invocation violates the launch contract."""


@dataclass(frozen=True)
class MetalSourceRuntimeBridgeArtifacts:
    """Artifact state for a runtime-source Metal executor bridge."""

    status: str
    symbol: str
    source_path: str
    source: str
    object_path: str | None = None
    library_path: str | None = None
    validated_symbol: str | None = None
    reason: str = ""
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False
    claim_mode: str = "Metal runtime-source bridge artifacts, not executed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "claim_mode": self.claim_mode,
            "symbol": self.symbol,
            "source_path": self.source_path,
            "object_path": self.object_path,
            "library_path": self.library_path,
            "validated_symbol": self.validated_symbol,
            "reason": self.reason,
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
        }


@dataclass(frozen=True)
class MetalSourceRuntimeInvocationResult:
    """Result of invoking or ABI-validating the runtime-source bridge."""

    status: str
    return_code: int
    bridge_function_called: bool
    fence_completed: bool
    injected_cdll_factory: bool
    runtime_launch_executed: bool
    runtime_source_compiled: bool
    whole_program_gpu: bool = False
    bridge_error: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "return_code": self.return_code,
            "bridge_function_called": self.bridge_function_called,
            "fence_completed": self.fence_completed,
            "injected_cdll_factory": self.injected_cdll_factory,
            "runtime_launch_executed": self.runtime_launch_executed,
            "runtime_source_compiled": self.runtime_source_compiled,
            "whole_program_gpu": self.whole_program_gpu,
            "bridge_error": self.bridge_error,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MetalSourceRuntimePackageResult:
    """End-to-end runtime-source package/run result for one Kernel IR launch."""

    status: str
    package_status: str
    module_name: str
    artifact_dir: str
    finalize: MetalFinalizeResult
    launch_plan: MetalLaunchPlan
    native_buffer_runtime: MetalNativeBufferRuntimeArtifacts
    source_bridge: MetalSourceRuntimeBridgeArtifacts
    invocation: MetalSourceRuntimeInvocationResult | None = None
    matrix_write: MetalMatrixTransferSet | None = None
    cpu_comparison: MetalCpuOracleComparisonResult | None = None
    allocation_snapshot: dict[str, Any] | None = None
    allocations_released: bool = False
    reason: str = ""
    runtime_launch_executed: bool = False
    runtime_source_compiled: bool = False
    whole_program_gpu: bool = False
    claim_mode: str = "Metal runtime-source package execution"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "claim_mode": self.claim_mode,
            "package_status": self.package_status,
            "module_name": self.module_name,
            "artifact_dir": self.artifact_dir,
            "runtime_launch_executed": self.runtime_launch_executed,
            "runtime_source_compiled": self.runtime_source_compiled,
            "whole_program_gpu": self.whole_program_gpu,
            "reason": self.reason,
            "finalize": self.finalize.to_dict(),
            "launch_plan": self.launch_plan.to_dict(),
            "native_buffer_runtime": self.native_buffer_runtime.to_dict(),
            "source_bridge": self.source_bridge.to_dict(),
            "matrix_write": self.matrix_write.to_dict() if self.matrix_write else None,
            "invocation": self.invocation.to_dict() if self.invocation else None,
            "cpu_comparison": (
                self.cpu_comparison.to_dict() if self.cpu_comparison else None
            ),
            "allocation_snapshot": self.allocation_snapshot,
            "allocations_released": self.allocations_released,
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


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def _runtime_source_package_artifact_paths(
    result: MetalSourceRuntimePackageResult,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for name, path in sorted(result.finalize.artifact_paths.items()):
        artifacts[f"finalize.{name}"] = path
    artifacts["native_buffer_runtime.source"] = result.native_buffer_runtime.source_path
    if result.native_buffer_runtime.object_path is not None:
        artifacts["native_buffer_runtime.object"] = result.native_buffer_runtime.object_path
    if result.native_buffer_runtime.library_path is not None:
        artifacts["native_buffer_runtime.library"] = result.native_buffer_runtime.library_path
    artifacts["source_bridge.source"] = result.source_bridge.source_path
    if result.source_bridge.object_path is not None:
        artifacts["source_bridge.object"] = result.source_bridge.object_path
    if result.source_bridge.library_path is not None:
        artifacts["source_bridge.library"] = result.source_bridge.library_path
    return artifacts


def metal_source_runtime_package_manifest_dict(
    result: MetalSourceRuntimePackageResult,
) -> dict[str, Any]:
    """Return a deterministic manifest for a runtime-source package result."""
    artifact_records: dict[str, dict[str, Any]] = {}
    for name, raw_path in _runtime_source_package_artifact_paths(result).items():
        path = Path(raw_path)
        if not path.is_file():
            raise MetalSourceRuntimeError(
                f"runtime-source package artifact {name!r} does not exist: {path}"
            )
        digest, size = _sha256_file(path)
        artifact_records[name] = {
            "path": str(path),
            "sha256": digest,
            "nbytes": size,
        }

    return {
        "manifest_version": 1,
        "status": result.status,
        "claim_mode": result.claim_mode,
        "module_name": result.module_name,
        "artifact_dir": result.artifact_dir,
        "runtime_launch_executed": result.runtime_launch_executed,
        "runtime_source_compiled": result.runtime_source_compiled,
        "whole_program_gpu": result.whole_program_gpu,
        "artifacts": artifact_records,
        "result": result.to_dict(),
    }


def write_metal_source_runtime_package_manifest(
    result: MetalSourceRuntimePackageResult,
    manifest_path: str | Path | None = None,
) -> Path:
    """Write a deterministic JSON manifest for a runtime-source package result."""
    path = (
        Path(manifest_path)
        if manifest_path is not None
        else Path(result.artifact_dir) / "metal_source_runtime_package_manifest.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    data = metal_source_runtime_package_manifest_dict(result)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _require_false(data: Mapping[str, Any], key: str, *, context: str) -> None:
    if data.get(key) is not False:
        raise MetalSourceRuntimeError(f"{context} claims {key}")


def _verify_runtime_source_claims(data: Mapping[str, Any]) -> None:
    _require_false(data, "whole_program_gpu", context="runtime-source manifest")
    result = data.get("result")
    if not isinstance(result, dict):
        raise MetalSourceRuntimeError("runtime-source manifest has no result record")
    _require_false(result, "whole_program_gpu", context="runtime-source result")
    if result.get("status") != data.get("status"):
        raise MetalSourceRuntimeError("runtime-source manifest status drift")
    if result.get("runtime_launch_executed") != data.get("runtime_launch_executed"):
        raise MetalSourceRuntimeError("runtime-source manifest launch-claim drift")
    if result.get("runtime_source_compiled") != data.get("runtime_source_compiled"):
        raise MetalSourceRuntimeError("runtime-source manifest compile-claim drift")

    invocation = result.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}
    comparison = result.get("cpu_comparison")
    comparison = comparison if isinstance(comparison, dict) else {}

    if data.get("status") == STATUS_SOURCE_RUNTIME_PACKAGE_EXECUTED:
        if data.get("runtime_launch_executed") is not True:
            raise MetalSourceRuntimeError(
                "runtime-source executed manifest is missing runtime_launch_executed"
            )
        if data.get("runtime_source_compiled") is not True:
            raise MetalSourceRuntimeError(
                "runtime-source executed manifest is missing runtime_source_compiled"
            )
        required = {
            "status": STATUS_SOURCE_RUNTIME_INVOKED,
            "fence_completed": True,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "injected_cdll_factory": False,
        }
        for key, expected in required.items():
            if invocation.get(key) != expected:
                raise MetalSourceRuntimeError(
                    f"runtime-source executed manifest has bad invocation {key}"
                )
        if comparison.get("status") != "metal_cpu_oracle_match":
            raise MetalSourceRuntimeError(
                "runtime-source executed manifest lacks CPU-oracle match"
            )
        if comparison.get("runtime_launch_executed") is not True:
            raise MetalSourceRuntimeError(
                "runtime-source CPU-oracle match does not carry launch claim"
            )
        return

    if data.get("runtime_launch_executed") is not False:
        raise MetalSourceRuntimeError(
            "runtime-source manifest claims launch without executed status"
        )
    if data.get("runtime_source_compiled") is not False:
        raise MetalSourceRuntimeError(
            "runtime-source manifest claims runtime-source compile without executed status"
        )
    if data.get("status") == STATUS_SOURCE_RUNTIME_PACKAGE_ABI_VALIDATED:
        if invocation.get("status") != STATUS_SOURCE_RUNTIME_INVOCATION_ABI_VALIDATED:
            raise MetalSourceRuntimeError(
                "runtime-source ABI manifest lacks ABI invocation validation"
            )
        if invocation.get("injected_cdll_factory") is not True:
            raise MetalSourceRuntimeError(
                "runtime-source ABI manifest must use an injected CDLL factory"
            )


def verify_metal_source_runtime_package_manifest(
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Verify a runtime-source package manifest and its artifact hashes."""
    path = Path(manifest_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MetalSourceRuntimeError(
            f"cannot read runtime-source package manifest {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MetalSourceRuntimeError(
            f"bad runtime-source package manifest JSON {path}: {exc}"
        ) from exc

    if data.get("manifest_version") != 1:
        raise MetalSourceRuntimeError(
            f"unsupported runtime-source manifest version {data.get('manifest_version')!r}"
        )
    _verify_runtime_source_claims(data)

    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise MetalSourceRuntimeError("runtime-source manifest has no artifact hashes")
    for name, record in artifacts.items():
        if not isinstance(record, dict):
            raise MetalSourceRuntimeError(f"bad runtime-source artifact record {name!r}")
        raw_artifact_path = record.get("path")
        expected_hash = record.get("sha256")
        expected_nbytes = record.get("nbytes")
        if not isinstance(raw_artifact_path, str) or not isinstance(expected_hash, str):
            raise MetalSourceRuntimeError(f"bad runtime-source artifact record {name!r}")
        if not isinstance(expected_nbytes, int) or expected_nbytes < 0:
            raise MetalSourceRuntimeError(
                f"bad runtime-source artifact size for {name!r}: {expected_nbytes!r}"
            )
        artifact_path = Path(raw_artifact_path)
        if not artifact_path.is_file():
            raise MetalSourceRuntimeError(
                f"runtime-source package artifact {name!r} is missing: {artifact_path}"
            )
        digest, size = _sha256_file(artifact_path)
        if size != expected_nbytes:
            raise MetalSourceRuntimeError(
                f"runtime-source artifact {name!r} size changed: "
                f"expected {expected_nbytes}, got {size}"
            )
        if digest != expected_hash:
            raise MetalSourceRuntimeError(
                f"runtime-source artifact {name!r} sha256 changed"
            )
    return data


def _objc_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _u3(values: tuple[int, int, int]) -> tuple[str, str, str]:
    return tuple(f"{value}u" for value in values)  # type: ignore[return-value]


def metal_source_runtime_bridge_symbol(
    plan: MetalLaunchPlan,
    *,
    function_name: str | None = None,
) -> str:
    """Return the C symbol for a runtime-source Metal bridge."""
    return _c_identifier(function_name or f"{plan.launcher_symbol}_source_runtime_bridge")


def emit_metal_source_runtime_bridge_source(
    plan: MetalLaunchPlan,
    *,
    function_name: str | None = None,
) -> str:
    """Emit an Objective-C bridge that compiles Metal source at runtime."""
    symbol = metal_source_runtime_bridge_symbol(plan, function_name=function_name)
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
        "#include <stdio.h>",
        "#include <string.h>",
        "",
        "typedef void (*pcc_metal_fence_complete_fn)(void *ctx);",
        "static char pcc_last_error[4096];",
        "",
        "static void pcc_clear_last_error(void) {",
        "  pcc_last_error[0] = '\\0';",
        "}",
        "",
        "static void pcc_set_last_error(const char *fallback, NSError *error) {",
        "  const char *message = fallback;",
        "  if (error != nil && [error localizedDescription] != nil) {",
        "    message = [[error localizedDescription] UTF8String];",
        "  }",
        "  if (message == NULL) { message = \"unknown Metal runtime-source error\"; }",
        "  snprintf(pcc_last_error, sizeof(pcc_last_error), \"%s\", message);",
        "}",
        "",
        f"int64_t {symbol}_copy_last_error(char *out, uint64_t out_len) {{",
        "  if (out == NULL || out_len == 0) { return 2; }",
        "  size_t n = strnlen(pcc_last_error, sizeof(pcc_last_error));",
        "  if (n >= out_len) { n = (size_t)out_len - 1; }",
        "  memcpy(out, pcc_last_error, n);",
        "  out[n] = '\\0';",
        "  return 0;",
        "}",
        "",
        f"int64_t {symbol}(",
        "    const char *metal_source,",
        "    uint64_t metal_source_len,",
        "    void *const *buffer_handles,",
        "    const void *const *scalar_values,",
        "    pcc_metal_fence_complete_fn fence_complete,",
        "    void *fence_ctx,",
        "    bool wait_until_completed)",
        "{",
        "  @autoreleasepool {",
        "    pcc_clear_last_error();",
        "    if (metal_source == NULL || metal_source_len == 0) {",
        "      pcc_set_last_error(\"missing Metal source\", nil);",
        "      return 2;",
        "    }",
        "    id<MTLDevice> device = MTLCreateSystemDefaultDevice();",
        "    if (device == nil) {",
        "      pcc_set_last_error(\"MTLCreateSystemDefaultDevice returned nil\", nil);",
        "      return 3;",
        "    }",
        "    NSString *source = [[NSString alloc]",
        "        initWithBytes:metal_source",
        "              length:(NSUInteger)metal_source_len",
        "            encoding:NSUTF8StringEncoding];",
        "    if (source == nil) {",
        "      pcc_set_last_error(\"Metal source is not valid UTF-8\", nil);",
        "      return 13;",
        "    }",
        "    NSError *error = nil;",
        "    id<MTLLibrary> library = [device newLibraryWithSource:source",
        "                                                     options:nil",
        "                                                       error:&error];",
        "    if (library == nil) {",
        "      pcc_set_last_error(\"newLibraryWithSource failed\", error);",
        "      return 4;",
        "    }",
        f"    id<MTLFunction> function = [library newFunctionWithName:@\"{kernel}\"];",
        "    if (function == nil) {",
        "      pcc_set_last_error(\"newFunctionWithName failed\", nil);",
        "      return 5;",
        "    }",
        "    id<MTLComputePipelineState> pipeline =",
        "        [device newComputePipelineStateWithFunction:function error:&error];",
        "    if (pipeline == nil) {",
        "      pcc_set_last_error(\"newComputePipelineStateWithFunction failed\", error);",
        "      return 6;",
        "    }",
        "    id<MTLCommandQueue> queue = [device newCommandQueue];",
        "    if (queue == nil) {",
        "      pcc_set_last_error(\"newCommandQueue failed\", nil);",
        "      return 7;",
        "    }",
        "    id<MTLCommandBuffer> command_buffer = [queue commandBuffer];",
        "    if (command_buffer == nil) {",
        "      pcc_set_last_error(\"commandBuffer returned nil\", nil);",
        "      return 8;",
        "    }",
        "    id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];",
        "    if (encoder == nil) {",
        "      pcc_set_last_error(\"computeCommandEncoder returned nil\", nil);",
        "      return 8;",
        "    }",
        "    [encoder setComputePipelineState:pipeline];",
    ]

    for ordinal, arg in enumerate(buffer_args):
        ident = _c_identifier(arg.name)
        lines.extend(
            [
                f"    if (buffer_handles == NULL || buffer_handles[{ordinal}] == NULL) {{",
                "      pcc_set_last_error(\"missing native MTLBuffer handle\", nil);",
                "      return 10;",
                "    }",
                f"    id<MTLBuffer> pcc_buf_{ident} = (__bridge id<MTLBuffer>)buffer_handles[{ordinal}];",
                f"    [encoder setBuffer:pcc_buf_{ident} offset:0 atIndex:{arg.index}];",
            ]
        )

    for ordinal, arg in enumerate(scalar_args):
        ctype = _SCALAR_C_TYPES.get(arg.dtype)
        if ctype is None:
            raise MetalSourceRuntimeError(f"no runtime-source scalar type for {arg.dtype!r}")
        ident = _c_identifier(arg.name)
        lines.extend(
            [
                f"    if (scalar_values == NULL || scalar_values[{ordinal}] == NULL) {{",
                "      pcc_set_last_error(\"missing scalar value pointer\", nil);",
                "      return 11;",
                "    }",
                f"    const {ctype} *pcc_scalar_{ident} = (const {ctype} *)scalar_values[{ordinal}];",
                f"    [encoder setBytes:pcc_scalar_{ident} length:sizeof({ctype}) atIndex:{arg.index}];",
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
            "      if ([command_buffer error] != nil) {",
            "        pcc_set_last_error(\"Metal command buffer failed\", [command_buffer error]);",
            "        return 12;",
            "      }",
            "    }",
            "    return 0;",
            "  }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def build_metal_source_runtime_bridge_artifacts(
    plan: MetalLaunchPlan,
    artifact_dir: str | Path,
    *,
    function_name: str | None = None,
    compile_bridge: bool = False,
    link_bridge_library: bool = False,
    validate_bridge_library: bool = False,
    compiler: Callable[..., Path] | None = None,
    linker: Callable[..., Path] | None = None,
    loader: Callable[..., str] | None = None,
    timeout: float = 30.0,
) -> MetalSourceRuntimeBridgeArtifacts:
    """Write and optionally build a runtime-source Metal executor bridge."""
    if validate_bridge_library and not link_bridge_library:
        raise MetalSourceRuntimeError(
            "validate_bridge_library=True requires link_bridge_library=True"
        )
    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol = metal_source_runtime_bridge_symbol(plan, function_name=function_name)
    source = emit_metal_source_runtime_bridge_source(plan, function_name=function_name)
    source_path = out_dir / f"{_c_identifier(plan.kernel_entry)}_metal_source_runtime_bridge.m"
    object_path = source_path.with_suffix(".o")
    library_path = source_path.with_suffix(".dylib")
    source_path.write_text(source, encoding="utf-8")

    if not compile_bridge:
        return MetalSourceRuntimeBridgeArtifacts(
            status=STATUS_SOURCE_RUNTIME_BRIDGE_SOURCE_ONLY,
            symbol=symbol,
            source_path=str(source_path),
            source=source,
            reason=(
                "Runtime-source bridge written; no object/library was produced "
                "and no command buffer was committed."
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
        return MetalSourceRuntimeBridgeArtifacts(
            status=STATUS_SKIPPED_WITH_REASON,
            symbol=symbol,
            source_path=str(source_path),
            source=source,
            object_path=str(object_path),
            reason=f"Runtime-source bridge compiler unavailable: {exc}",
        )
    except MetalCompileError as exc:
        raise MetalSourceRuntimeError(
            f"runtime-source bridge failed to compile: {exc}"
        ) from exc
    compiled = Path(compiled_path)
    if not compiled.is_file():
        raise MetalSourceRuntimeError(
            f"runtime-source bridge compiler returned no object: {compiled}"
        )

    if not link_bridge_library:
        return MetalSourceRuntimeBridgeArtifacts(
            status=STATUS_SOURCE_RUNTIME_BRIDGE_OBJECT_PRODUCED,
            symbol=symbol,
            source_path=str(source_path),
            source=source,
            object_path=str(compiled),
            reason=(
                "Runtime-source bridge object produced; no dylib was linked "
                "and no command buffer was committed."
            ),
        )

    if linker is None:
        from pcc.backend.metal import link_metal_runtime_bridge_dylib

        linker = link_metal_runtime_bridge_dylib
    try:
        linked_path = linker(
            library_path,
            object_path=compiled,
            timeout=timeout,
        )
    except MetalToolchainUnavailable as exc:
        return MetalSourceRuntimeBridgeArtifacts(
            status=STATUS_SKIPPED_WITH_REASON,
            symbol=symbol,
            source_path=str(source_path),
            source=source,
            object_path=str(compiled),
            library_path=str(library_path),
            reason=f"Runtime-source bridge linker unavailable: {exc}",
        )
    except MetalCompileError as exc:
        raise MetalSourceRuntimeError(
            f"runtime-source bridge dylib failed to link: {exc}"
        ) from exc
    linked = Path(linked_path)
    if not linked.is_file():
        raise MetalSourceRuntimeError(
            f"runtime-source bridge linker returned no library: {linked}"
        )

    if not validate_bridge_library:
        return MetalSourceRuntimeBridgeArtifacts(
            status=STATUS_SOURCE_RUNTIME_BRIDGE_LIBRARY_PRODUCED,
            symbol=symbol,
            source_path=str(source_path),
            source=source,
            object_path=str(compiled),
            library_path=str(linked),
            reason=(
                "Runtime-source bridge dylib produced; symbol load was not "
                "requested and no command buffer was committed."
            ),
        )

    if loader is None:
        from pcc.backend.metal import validate_dynamic_library_symbol

        def loader(path: Path, *, symbol: str) -> str:
            return validate_dynamic_library_symbol(path, symbol)

    try:
        validated = loader(linked, symbol=symbol)
    except MetalCompileError as exc:
        raise MetalSourceRuntimeError(
            f"runtime-source bridge dylib failed symbol validation: {exc}"
        ) from exc
    return MetalSourceRuntimeBridgeArtifacts(
        status=STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED,
        symbol=symbol,
        source_path=str(source_path),
        source=source,
        object_path=str(compiled),
        library_path=str(linked),
        validated_symbol=validated,
        reason=(
            "Runtime-source bridge dylib produced and symbol load validated; "
            "no command buffer was committed."
        ),
    )


def _load_bridge_function(
    library_path: str | Path,
    symbol: str,
    *,
    cdll_factory: Callable[[str], Any] | None,
) -> tuple[Any, Any | None]:
    lib_path = Path(library_path)
    if not lib_path.is_file():
        raise MetalSourceRuntimeError(f"runtime-source bridge dylib is missing: {lib_path}")
    try:
        load_library = cdll_factory if cdll_factory is not None else ctypes.CDLL
        lib = load_library(str(lib_path))
    except OSError as exc:
        raise MetalSourceRuntimeError(
            f"runtime-source bridge dylib load failed: {exc}"
        ) from exc
    try:
        fn = getattr(lib, symbol)
    except AttributeError as exc:
        raise MetalSourceRuntimeError(
            f"runtime-source bridge dylib does not export {symbol!r}"
        ) from exc
    fn.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_bool,
    ]
    fn.restype = ctypes.c_int64
    error_fn = None
    try:
        error_fn = getattr(lib, f"{symbol}_copy_last_error")
    except AttributeError:
        error_fn = None
    if error_fn is not None:
        error_fn.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        error_fn.restype = ctypes.c_int64
    return fn, error_fn


def _read_bridge_error(error_fn: Any | None) -> str | None:
    if error_fn is None:
        return None
    buf = ctypes.create_string_buffer(4096)
    try:
        rc = int(error_fn(buf, ctypes.c_uint64(len(buf))))
    except Exception:
        return None
    if rc != 0:
        return None
    text = buf.value.decode("utf-8", errors="replace").strip()
    return text or None


def invoke_metal_source_runtime_bridge(
    *,
    plan: MetalLaunchPlan,
    metal_source: str | bytes,
    bridge_library_path: str | Path,
    symbol: str | None = None,
    native_buffer_bindings: MetalNativeBufferBindingSet,
    fence: PccFenceToken | None = None,
    wait_until_completed: bool = True,
    cdll_factory: Callable[[str], Any] | None = None,
) -> MetalSourceRuntimeInvocationResult:
    """Invoke a runtime-source Metal bridge.

    Injected CDLL calls are ABI validation only. Real calls returning zero are
    the first point that may claim command-buffer execution for this path.
    """
    resolved_symbol = symbol or metal_source_runtime_bridge_symbol(plan)
    if fence is None:
        raise MetalSourceRuntimeError(
            "runtime-source Metal invocation requires a PccFenceToken"
        )
    try:
        call_plan = build_metal_source_runtime_call_plan(
            launch_plan=plan,
            metal_source=metal_source,
            bridge_library_path=bridge_library_path,
            symbol=resolved_symbol,
            native_buffer_bindings=native_buffer_bindings,
            wait_until_completed=wait_until_completed,
        )
    except ValueError as exc:
        raise MetalSourceRuntimeError(str(exc)) from exc
    bridge_fn, error_fn = _load_bridge_function(
        bridge_library_path,
        resolved_symbol,
        cdll_factory=cdll_factory,
    )

    buffer_ptrs = [
        ctypes.c_void_p(slot.native_mtlbuffer_ptr)
        for slot in call_plan.buffer_slots
    ]
    buffer_array_type = ctypes.c_void_p * max(1, len(buffer_ptrs))
    buffer_array = buffer_array_type(*(buffer_ptrs or [ctypes.c_void_p()]))

    scalar_payload = ctypes.create_string_buffer(call_plan.scalar_payload)
    scalar_ptrs = [
        ctypes.cast(ctypes.byref(scalar_payload, slot.abi_offset), ctypes.c_void_p)
        for slot in call_plan.scalar_slots
    ]
    scalar_array_type = ctypes.c_void_p * max(1, len(scalar_ptrs))
    scalar_array = scalar_array_type(*(scalar_ptrs or [ctypes.c_void_p()]))

    rc = int(
        bridge_fn(
            call_plan.source_bytes,
            ctypes.c_uint64(call_plan.source_nbytes),
            buffer_array,
            scalar_array,
            ctypes.c_void_p(0),
            ctypes.c_void_p(0),
            ctypes.c_bool(call_plan.wait_until_completed),
        )
    )
    if rc == 0 and call_plan.wait_until_completed:
        fence.complete()

    injected = cdll_factory is not None
    if rc == 0 and injected:
        return MetalSourceRuntimeInvocationResult(
            status=STATUS_SOURCE_RUNTIME_INVOCATION_ABI_VALIDATED,
            return_code=rc,
            bridge_function_called=True,
            fence_completed=fence.completed if fence is not None else False,
            injected_cdll_factory=True,
            runtime_launch_executed=False,
            runtime_source_compiled=False,
            reason=(
                "Injected CDLL bridge call validated runtime-source ABI packing; "
                "no GPU execution claimed."
            ),
        )
    if rc == 0:
        return MetalSourceRuntimeInvocationResult(
            status=STATUS_SOURCE_RUNTIME_INVOKED,
            return_code=rc,
            bridge_function_called=True,
            fence_completed=fence.completed if fence is not None else False,
            injected_cdll_factory=False,
            runtime_launch_executed=True,
            runtime_source_compiled=True,
            reason=(
                "Runtime-source Metal bridge returned success after command "
                "buffer submission and waitUntilCompleted."
            ),
        )
    if rc == 3 and not injected:
        bridge_error = _read_bridge_error(error_fn)
        return MetalSourceRuntimeInvocationResult(
            status=STATUS_SKIPPED_WITH_REASON,
            return_code=rc,
            bridge_function_called=True,
            fence_completed=fence.completed if fence is not None else False,
            injected_cdll_factory=False,
            runtime_launch_executed=False,
            runtime_source_compiled=False,
            bridge_error=bridge_error,
            reason="MTLCreateSystemDefaultDevice returned nil; no Metal device available.",
        )
    bridge_error = _read_bridge_error(error_fn)
    detail = f": {bridge_error}" if bridge_error else ""
    return MetalSourceRuntimeInvocationResult(
        status=STATUS_SOURCE_RUNTIME_INVOCATION_FAILED,
        return_code=rc,
        bridge_function_called=True,
        fence_completed=fence.completed if fence is not None else False,
        injected_cdll_factory=injected,
        runtime_launch_executed=False,
        runtime_source_compiled=False,
        bridge_error=bridge_error,
        reason=(
            f"Runtime-source Metal bridge returned non-zero rc={rc}{detail}; "
            "no successful launch claimed."
        ),
    )


def _package_result(
    *,
    status: str,
    package: Any,
    native_buffer_runtime: MetalNativeBufferRuntimeArtifacts,
    source_bridge: MetalSourceRuntimeBridgeArtifacts,
    invocation: MetalSourceRuntimeInvocationResult | None = None,
    matrix_write: MetalMatrixTransferSet | None = None,
    cpu_comparison: MetalCpuOracleComparisonResult | None = None,
    allocation_set: MetalNativeBufferAllocationSet | None = None,
    reason: str,
) -> MetalSourceRuntimePackageResult:
    allocation_snapshot = allocation_set.to_dict() if allocation_set is not None else None
    runtime_launch_executed = bool(
        invocation is not None
        and invocation.runtime_launch_executed
        and cpu_comparison is not None
        and cpu_comparison.runtime_launch_executed
    )
    return MetalSourceRuntimePackageResult(
        status=status,
        package_status=package.status,
        module_name=package.module_name,
        artifact_dir=package.artifact_dir,
        finalize=package.finalize,
        launch_plan=package.launch_plan,
        native_buffer_runtime=native_buffer_runtime,
        source_bridge=source_bridge,
        invocation=invocation,
        matrix_write=matrix_write,
        cpu_comparison=cpu_comparison,
        allocation_snapshot=allocation_snapshot,
        allocations_released=allocation_set.released if allocation_set is not None else False,
        reason=reason,
        runtime_launch_executed=runtime_launch_executed,
        runtime_source_compiled=bool(
            invocation is not None and invocation.runtime_source_compiled
        ),
    )


def run_metal_source_runtime_package(
    module: Any,
    packed_args: Any,
    artifact_dir: str | Path,
    *,
    metal_source: str | bytes,
    input_matrices: Mapping[str, object],
    cpu_reference: Any,
    output_name: str | None = None,
    entry: str | None = None,
    zero_fill_unprovided: bool = True,
    wait_until_completed: bool = True,
    timeout: float = 30.0,
    compile_package_bridge: bool = False,
    package_bridge_compiler: Callable[..., Path] | None = None,
    native_buffer_compiler: Callable[..., Path] | None = None,
    native_buffer_linker: Callable[..., Path] | None = None,
    native_buffer_loader: Callable[..., str] | None = None,
    source_bridge_compiler: Callable[..., Path] | None = None,
    source_bridge_linker: Callable[..., Path] | None = None,
    source_bridge_loader: Callable[..., str] | None = None,
    buffer_cdll_factory: Callable[[str], Any] | None = None,
    bridge_cdll_factory: Callable[[str], Any] | None = None,
) -> MetalSourceRuntimePackageResult:
    """Build, run, and verify one runtime-source Metal package.

    This is the first-class API for the ``newLibraryWithSource`` route. It keeps
    the existing ``MetalKernelPackage`` non-executing: the execution claim lives
    only in the returned ``MetalSourceRuntimePackageResult`` and only when a real
    non-injected bridge call returned success and the readback matched the CPU
    oracle.
    """
    from pcc.kernel_ir.metal_buffer import (
        build_metal_native_buffer_runtime_artifacts,
    )
    from pcc.kernel_ir.metal_package import build_metal_kernel_package

    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    package = build_metal_kernel_package(
        module,
        packed_args,
        out_dir,
        entry=entry,
        compile_bridge=compile_package_bridge,
        bridge_compiler=package_bridge_compiler,
        timeout=timeout,
    )
    native_buffer_runtime = build_metal_native_buffer_runtime_artifacts(
        out_dir / "native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
        compiler=native_buffer_compiler,
        linker=native_buffer_linker,
        loader=native_buffer_loader,
        timeout=timeout,
    )
    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        out_dir / "source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        compiler=source_bridge_compiler,
        linker=source_bridge_linker,
        loader=source_bridge_loader,
        timeout=timeout,
    )

    if native_buffer_runtime.status == STATUS_SKIPPED_WITH_REASON:
        return _package_result(
            status=STATUS_SKIPPED_WITH_REASON,
            package=package,
            native_buffer_runtime=native_buffer_runtime,
            source_bridge=source_bridge,
            reason=native_buffer_runtime.reason,
        )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        return _package_result(
            status=STATUS_SKIPPED_WITH_REASON,
            package=package,
            native_buffer_runtime=native_buffer_runtime,
            source_bridge=source_bridge,
            reason=source_bridge.reason,
        )
    if native_buffer_runtime.library_path is None:
        raise MetalSourceRuntimeError("native buffer runtime did not produce a dylib")
    if source_bridge.library_path is None:
        raise MetalSourceRuntimeError("runtime-source bridge did not produce a dylib")

    return run_metal_source_runtime_prebuilt_package(
        package,
        native_buffer_runtime,
        source_bridge,
        metal_source=metal_source,
        input_matrices=input_matrices,
        cpu_reference=cpu_reference,
        output_name=output_name,
        zero_fill_unprovided=zero_fill_unprovided,
        wait_until_completed=wait_until_completed,
        buffer_cdll_factory=buffer_cdll_factory,
        bridge_cdll_factory=bridge_cdll_factory,
    )


def run_metal_source_runtime_prebuilt_package(
    package: Any,
    native_buffer_runtime: MetalNativeBufferRuntimeArtifacts,
    source_bridge: MetalSourceRuntimeBridgeArtifacts,
    *,
    metal_source: str | bytes,
    input_matrices: Mapping[str, object],
    cpu_reference: Any,
    output_name: str | None = None,
    zero_fill_unprovided: bool = True,
    wait_until_completed: bool = True,
    buffer_cdll_factory: Callable[[str], Any] | None = None,
    bridge_cdll_factory: Callable[[str], Any] | None = None,
) -> MetalSourceRuntimePackageResult:
    """Run a runtime-source package from prebuilt bridge/runtime artifacts.

    This is the pcc1-facing execution boundary: artifact production may happen
    outside the pcc1 process, while allocation, matrix transfer, bridge invoke,
    readback verification, and release still use the same launcher path.
    """
    from pcc.kernel_ir.metal_buffer import allocate_metal_native_buffers_for_plan
    from pcc.kernel_ir.metal_tensor import write_metal_launch_matrices
    from pcc.kernel_ir.metal_verify import (
        verify_metal_launch_output_against_cpu_reference,
    )

    if native_buffer_runtime.status == STATUS_SKIPPED_WITH_REASON:
        return _package_result(
            status=STATUS_SKIPPED_WITH_REASON,
            package=package,
            native_buffer_runtime=native_buffer_runtime,
            source_bridge=source_bridge,
            reason=native_buffer_runtime.reason,
        )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        return _package_result(
            status=STATUS_SKIPPED_WITH_REASON,
            package=package,
            native_buffer_runtime=native_buffer_runtime,
            source_bridge=source_bridge,
            reason=source_bridge.reason,
        )
    if native_buffer_runtime.library_path is None:
        raise MetalSourceRuntimeError("native buffer runtime did not produce a dylib")
    if source_bridge.library_path is None:
        raise MetalSourceRuntimeError("runtime-source bridge did not produce a dylib")

    allocation_set: MetalNativeBufferAllocationSet | None = None
    matrix_write = None
    invocation = None
    comparison = None
    status = STATUS_SOURCE_RUNTIME_PACKAGE_FAILED
    reason = "Runtime-source package execution did not complete."
    try:
        allocation_set = allocate_metal_native_buffers_for_plan(
            native_buffer_runtime.library_path,
            package.launch_plan,
            cdll_factory=buffer_cdll_factory,
        )
        if allocation_set.status == STATUS_SKIPPED_WITH_REASON:
            status = STATUS_SKIPPED_WITH_REASON
            reason = allocation_set.reason
        else:
            if allocation_set.binding_set is None:
                raise MetalSourceRuntimeError(
                    "native buffer allocation did not produce bindings"
                )
            matrix_write = write_metal_launch_matrices(
                native_buffer_runtime.library_path,
                allocation_set,
                package.launch_plan,
                input_matrices,
                zero_fill_unprovided=zero_fill_unprovided,
                cdll_factory=buffer_cdll_factory,
            )
            fence = PccFenceToken()
            invocation = invoke_metal_source_runtime_bridge(
                plan=package.launch_plan,
                metal_source=metal_source,
                bridge_library_path=source_bridge.library_path,
                symbol=source_bridge.symbol,
                native_buffer_bindings=allocation_set.binding_set,
                fence=fence,
                wait_until_completed=wait_until_completed,
                cdll_factory=bridge_cdll_factory,
            )
            if invocation.status == STATUS_SKIPPED_WITH_REASON:
                status = STATUS_SKIPPED_WITH_REASON
                reason = invocation.reason
            elif invocation.runtime_launch_executed:
                comparison = verify_metal_launch_output_against_cpu_reference(
                    native_buffer_runtime.library_path,
                    allocation_set,
                    package.launch_plan,
                    cpu_reference,
                    output_name=output_name,
                    cdll_factory=buffer_cdll_factory,
                    runtime_launch_executed=True,
                )
                status = STATUS_SOURCE_RUNTIME_PACKAGE_EXECUTED
                reason = (
                    "Runtime-source Metal package submitted a command buffer, "
                    "completed the fence, and matched the CPU oracle."
                )
            elif invocation.status == STATUS_SOURCE_RUNTIME_INVOCATION_ABI_VALIDATED:
                status = STATUS_SOURCE_RUNTIME_PACKAGE_ABI_VALIDATED
                reason = (
                    "Injected runtime-source bridge validated package ABI only; "
                    "no GPU execution or CPU-oracle output claim."
                )
            else:
                status = STATUS_SOURCE_RUNTIME_PACKAGE_FAILED
                reason = invocation.reason
    finally:
        if allocation_set is not None:
            allocation_set.release_all()

    return _package_result(
        status=status,
        package=package,
        native_buffer_runtime=native_buffer_runtime,
        source_bridge=source_bridge,
        invocation=invocation,
        matrix_write=matrix_write,
        cpu_comparison=comparison,
        allocation_set=allocation_set,
        reason=reason,
    )


__all__ = [
    "MetalSourceRuntimeBridgeArtifacts",
    "MetalSourceRuntimeError",
    "MetalSourceRuntimeInvocationResult",
    "MetalSourceRuntimePackageResult",
    "STATUS_SKIPPED_WITH_REASON",
    "STATUS_SOURCE_RUNTIME_BRIDGE_LIBRARY_PRODUCED",
    "STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED",
    "STATUS_SOURCE_RUNTIME_BRIDGE_OBJECT_PRODUCED",
    "STATUS_SOURCE_RUNTIME_BRIDGE_SOURCE_ONLY",
    "STATUS_SOURCE_RUNTIME_INVOCATION_ABI_VALIDATED",
    "STATUS_SOURCE_RUNTIME_INVOCATION_FAILED",
    "STATUS_SOURCE_RUNTIME_INVOKED",
    "STATUS_SOURCE_RUNTIME_PACKAGE_ABI_VALIDATED",
    "STATUS_SOURCE_RUNTIME_PACKAGE_EXECUTED",
    "STATUS_SOURCE_RUNTIME_PACKAGE_FAILED",
    "build_metal_source_runtime_bridge_artifacts",
    "emit_metal_source_runtime_bridge_source",
    "invoke_metal_source_runtime_bridge",
    "metal_source_runtime_package_manifest_dict",
    "metal_source_runtime_bridge_symbol",
    "run_metal_source_runtime_prebuilt_package",
    "run_metal_source_runtime_package",
    "verify_metal_source_runtime_package_manifest",
    "write_metal_source_runtime_package_manifest",
]
