"""End-to-end non-executing Metal package manifest for Kernel IR.

The package builder ties together the current proof surfaces for one kernel:
CPU numeric oracle, Metal source artifact, launch packet, and host executor
bridge artifact. It still does not execute a GPU command buffer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from pcc.kernel_ir.cpu_reference import (
    CpuReferenceResult,
    execute_scalar_tiled_gemm_reference,
)
from pcc.kernel_ir.hmm_fence import PccPackedArgs
from pcc.kernel_ir.ir import KernelModule
from pcc.kernel_ir.metal_buffer import MetalNativeBufferBindingSet
from pcc.kernel_ir.metal_finalize import MetalFinalizeResult, finalize_metal
from pcc.kernel_ir.metal_launch import (
    MetalExecutorBridgeArtifacts,
    MetalLaunchPlan,
    build_metal_executor_bridge_artifacts,
    metal_executor_bridge_symbol,
    plan_metal_launch,
)
from pcc.kernel_ir.tirx_adapter import PlainTirModule, lower_to_plain_tir

STATUS_BRIDGE_LIBRARY_NOT_REQUESTED = "metal_bridge_library_not_requested"
STATUS_BRIDGE_LIBRARY_PRODUCED = "metal_bridge_library_produced"
STATUS_BRIDGE_LIBRARY_LOAD_NOT_REQUESTED = "metal_bridge_library_load_not_requested"
STATUS_BRIDGE_LIBRARY_LOAD_VALIDATED = "metal_bridge_library_load_validated"
STATUS_BRIDGE_INVOCATION_PACKET_READY = "metal_bridge_invocation_packet_ready"
STATUS_BRIDGE_INVOCATION_PACKET_NOT_READY = "metal_bridge_invocation_packet_not_ready"
STATUS_SKIPPED_WITH_REASON = "SKIPPED_WITH_REASON"


class MetalPackageError(ValueError):
    """The Metal package manifest could not be built honestly."""


@dataclass(frozen=True)
class MetalBridgeInvocationPacket:
    """Verified bridge-call ABI shape, without calling the bridge function.

    The packet records the C ABI slots consumed by the generated Objective-C
    bridge. It is intentionally not invocable until Kernel IR has both a
    produced metallib and native ``id<MTLBuffer>`` bindings for every buffer
    handle. A ``PccBufferHandle.handle_id`` is not a native Metal pointer.
    """

    status: str
    symbol: str
    bridge_library_path: str
    metallib_path: str | None
    metallib_available: bool
    buffer_handle_slots: tuple[dict[str, Any], ...]
    scalar_value_slots: tuple[dict[str, Any], ...]
    fence_callback_required: bool
    wait_until_completed: bool
    native_buffer_handles_ready: bool
    native_buffer_binding_status: str | None
    invocable: bool
    not_ready_reasons: tuple[str, ...]
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "bridge_library_path": self.bridge_library_path,
            "metallib_path": self.metallib_path,
            "metallib_available": self.metallib_available,
            "buffer_handle_slots": [dict(slot) for slot in self.buffer_handle_slots],
            "scalar_value_slots": [dict(slot) for slot in self.scalar_value_slots],
            "fence_callback_required": self.fence_callback_required,
            "wait_until_completed": self.wait_until_completed,
            "native_buffer_handles_ready": self.native_buffer_handles_ready,
            "native_buffer_binding_status": self.native_buffer_binding_status,
            "invocable": self.invocable,
            "not_ready_reasons": list(self.not_ready_reasons),
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
        }


@dataclass(frozen=True)
class MetalKernelPackage:
    """Non-executing package manifest for one Kernel IR Metal kernel."""

    status: str
    module_name: str
    artifact_dir: str
    finalize: MetalFinalizeResult
    launch_plan: MetalLaunchPlan
    bridge: MetalExecutorBridgeArtifacts
    cpu_reference: CpuReferenceResult | None = None
    bridge_library_status: str = STATUS_BRIDGE_LIBRARY_NOT_REQUESTED
    bridge_library_path: str | None = None
    bridge_library_produced: bool = False
    bridge_library_reason: str | None = None
    bridge_library_symbol: str | None = None
    bridge_library_load_status: str = STATUS_BRIDGE_LIBRARY_LOAD_NOT_REQUESTED
    bridge_library_load_validated: bool = False
    bridge_library_load_reason: str | None = None
    bridge_invocation_packet: MetalBridgeInvocationPacket | None = None
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False
    claim_mode: str = "Metal kernel package artifacts, not executed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "claim_mode": self.claim_mode,
            "module_name": self.module_name,
            "artifact_dir": self.artifact_dir,
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
            "cpu_reference": self.cpu_reference.to_dict() if self.cpu_reference else None,
            "finalize": self.finalize.to_dict(),
            "launch_plan": self.launch_plan.to_dict(),
            "bridge": self.bridge.to_dict(),
            "bridge_library": {
                "status": self.bridge_library_status,
                "path": self.bridge_library_path,
                "produced": self.bridge_library_produced,
                "reason": self.bridge_library_reason,
                "symbol": self.bridge_library_symbol,
                "load_status": self.bridge_library_load_status,
                "load_validated": self.bridge_library_load_validated,
                "load_reason": self.bridge_library_load_reason,
            },
            "bridge_invocation_packet": (
                self.bridge_invocation_packet.to_dict()
                if self.bridge_invocation_packet is not None
                else None
            ),
        }


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def _package_artifact_paths(package: MetalKernelPackage) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for name, path in sorted(package.finalize.artifact_paths.items()):
        artifacts[f"finalize.{name}"] = path
    artifacts["bridge.source"] = package.bridge.source_path
    if package.bridge.bridge_object_produced and package.bridge.object_path is not None:
        artifacts["bridge.object"] = package.bridge.object_path
    if package.bridge_library_produced and package.bridge_library_path is not None:
        artifacts["bridge.library"] = package.bridge_library_path
    return artifacts


def metal_kernel_package_manifest_dict(package: MetalKernelPackage) -> dict[str, Any]:
    """Return a deterministic manifest dict with hashes for produced artifacts."""
    artifact_records: dict[str, dict[str, Any]] = {}
    for name, raw_path in _package_artifact_paths(package).items():
        path = Path(raw_path)
        if not path.is_file():
            raise MetalPackageError(f"package artifact {name!r} does not exist: {path}")
        digest, size = _sha256_file(path)
        artifact_records[name] = {
            "path": str(path),
            "sha256": digest,
            "nbytes": size,
        }

    return {
        "manifest_version": 1,
        "status": package.status,
        "claim_mode": package.claim_mode,
        "module_name": package.module_name,
        "artifact_dir": package.artifact_dir,
        "runtime_launch_executed": package.runtime_launch_executed,
        "whole_program_gpu": package.whole_program_gpu,
        "artifacts": artifact_records,
        "package": package.to_dict(),
    }


def write_metal_kernel_package_manifest(
    package: MetalKernelPackage,
    manifest_path: str | Path | None = None,
) -> Path:
    """Write a deterministic JSON manifest for *package* and return its path."""
    path = (
        Path(manifest_path)
        if manifest_path is not None
        else Path(package.artifact_dir) / "metal_kernel_package_manifest.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    data = metal_kernel_package_manifest_dict(package)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify_metal_kernel_package_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Verify artifact hashes recorded in a package manifest.

    Returns the parsed manifest on success. Raises :class:`MetalPackageError`
    for missing files, changed sizes, changed hashes, or launch-claim drift.
    """
    path = Path(manifest_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MetalPackageError(f"cannot read package manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MetalPackageError(f"bad package manifest JSON {path}: {exc}") from exc

    if data.get("manifest_version") != 1:
        raise MetalPackageError(f"unsupported package manifest version {data.get('manifest_version')!r}")
    if data.get("runtime_launch_executed") is not False:
        raise MetalPackageError("package manifest claims runtime_launch_executed")
    if data.get("whole_program_gpu") is not False:
        raise MetalPackageError("package manifest claims whole_program_gpu")
    package = data.get("package")
    if isinstance(package, dict):
        if package.get("runtime_launch_executed") is not False:
            raise MetalPackageError("nested package claims runtime_launch_executed")
        if package.get("whole_program_gpu") is not False:
            raise MetalPackageError("nested package claims whole_program_gpu")
        packet = package.get("bridge_invocation_packet")
        if packet is not None:
            if not isinstance(packet, dict):
                raise MetalPackageError("bad bridge invocation packet record")
            if packet.get("runtime_launch_executed") is not False:
                raise MetalPackageError(
                    "bridge invocation packet claims runtime_launch_executed"
                )
            if packet.get("whole_program_gpu") is not False:
                raise MetalPackageError(
                    "bridge invocation packet claims whole_program_gpu"
                )
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise MetalPackageError("package manifest has no artifact hashes")
    for name, record in artifacts.items():
        if not isinstance(record, dict):
            raise MetalPackageError(f"bad artifact record {name!r}: {record!r}")
        raw_artifact_path = record.get("path")
        expected_hash = record.get("sha256")
        expected_nbytes = record.get("nbytes")
        if not isinstance(raw_artifact_path, str) or not isinstance(expected_hash, str):
            raise MetalPackageError(f"bad artifact record {name!r}: {record!r}")
        if not isinstance(expected_nbytes, int) or expected_nbytes < 0:
            raise MetalPackageError(f"bad artifact size for {name!r}: {expected_nbytes!r}")
        artifact_path = Path(raw_artifact_path)
        if not artifact_path.is_file():
            raise MetalPackageError(f"package artifact {name!r} is missing: {artifact_path}")
        digest, size = _sha256_file(artifact_path)
        if size != expected_nbytes:
            raise MetalPackageError(
                f"package artifact {name!r} size changed: expected {expected_nbytes}, got {size}"
            )
        if digest != expected_hash:
            raise MetalPackageError(f"package artifact {name!r} sha256 changed")
    return data


def _coerce_plain(module: KernelModule | PlainTirModule) -> PlainTirModule:
    return module if isinstance(module, PlainTirModule) else lower_to_plain_tir(module, target="metal")


def _make_bridge_invocation_packet(
    *,
    launch_plan: MetalLaunchPlan,
    bridge_library_path: str | None,
    bridge_library_symbol: str | None,
    bridge_library_load_validated: bool,
    wait_until_completed: bool,
    allow_missing_metallib: bool,
    native_buffer_bindings: MetalNativeBufferBindingSet | None,
) -> MetalBridgeInvocationPacket:
    if not bridge_library_load_validated or bridge_library_path is None or bridge_library_symbol is None:
        raise MetalPackageError(
            "bridge invocation packet requires validate_bridge_library=True, "
            "a produced bridge dylib, and a resolved bridge symbol"
        )

    metallib_available = bool(launch_plan.metallib_available and launch_plan.metallib_path)
    if not metallib_available and not allow_missing_metallib:
        raise MetalPackageError(
            "bridge invocation packet requires produced metallib; pass "
            "allow_missing_metallib=True only for non-invocable ABI-shape proof"
        )

    native_binding_by_handle_id: dict[int, dict[str, Any]] = {}
    if native_buffer_bindings is not None:
        if not native_buffer_bindings.native_buffer_handles_ready:
            raise MetalPackageError("native Metal buffer binding set is not ready")
        for binding in native_buffer_bindings.bindings:
            native_binding_by_handle_id[binding.handle_id] = binding.to_dict()

    buffer_slots: list[dict[str, Any]] = []
    scalar_slots: list[dict[str, Any]] = []
    expected_bound_handle_ids: set[int] = set()
    for arg in launch_plan.args:
        if arg.kind == "buffer":
            if arg.handle_id is None:
                raise MetalPackageError(f"buffer arg {arg.name!r} has no handle id")
            expected_bound_handle_ids.add(arg.handle_id)
            native_binding = native_binding_by_handle_id.get(arg.handle_id)
            native_bound = native_binding is not None
            slot: dict[str, Any] = {
                "bridge_ordinal": len(buffer_slots),
                "kernel_index": arg.index,
                "name": arg.name,
                "dtype": arg.dtype,
                "handle_id": arg.handle_id,
                "native_mtlbuffer_bound": native_bound,
            }
            if native_binding is not None:
                slot["source"] = native_binding["source"]
                slot["native_mtlbuffer_ptr"] = native_binding["native_mtlbuffer_ptr"]
            else:
                slot["source"] = (
                    "PccBufferHandle.handle_id only; not a native "
                    "id<MTLBuffer> pointer"
                )
            if arg.shape is not None:
                slot["shape"] = list(arg.shape)
            if arg.required_nbytes is not None:
                slot["required_nbytes"] = arg.required_nbytes
            if arg.provided_nbytes is not None:
                slot["provided_nbytes"] = arg.provided_nbytes
            buffer_slots.append(slot)
        elif arg.kind == "scalar":
            scalar_slots.append(
                {
                    "bridge_ordinal": len(scalar_slots),
                    "kernel_index": arg.index,
                    "name": arg.name,
                    "dtype": arg.dtype,
                    "scalar_value": arg.scalar_value,
                    "source": "POD scalar copied through scalar_values pointer slot",
                }
            )
        else:
            raise MetalPackageError(f"unsupported Metal bridge arg kind {arg.kind!r}")

    extra_bindings = set(native_binding_by_handle_id) - expected_bound_handle_ids
    if extra_bindings:
        raise MetalPackageError(
            f"native Metal buffer bindings do not belong to this launch plan: {sorted(extra_bindings)}"
        )

    native_buffer_handles_ready = all(
        bool(slot.get("native_mtlbuffer_bound")) for slot in buffer_slots
    )
    not_ready_reasons: list[str] = []
    if not metallib_available:
        not_ready_reasons.append("no produced metallib artifact is available")
    if not native_buffer_handles_ready:
        not_ready_reasons.append(
            "Kernel IR runtime has not bound PccBufferHandle handles to native "
            "id<MTLBuffer> pointers"
        )
    invocable = metallib_available and native_buffer_handles_ready
    status = (
        STATUS_BRIDGE_INVOCATION_PACKET_READY
        if invocable
        else STATUS_BRIDGE_INVOCATION_PACKET_NOT_READY
    )

    return MetalBridgeInvocationPacket(
        status=status,
        symbol=bridge_library_symbol,
        bridge_library_path=bridge_library_path,
        metallib_path=launch_plan.metallib_path,
        metallib_available=metallib_available,
        buffer_handle_slots=tuple(buffer_slots),
        scalar_value_slots=tuple(scalar_slots),
        fence_callback_required=launch_plan.fence_required_on_commit,
        wait_until_completed=wait_until_completed,
        native_buffer_handles_ready=native_buffer_handles_ready,
        native_buffer_binding_status=(
            native_buffer_bindings.status if native_buffer_bindings is not None else None
        ),
        invocable=invocable,
        not_ready_reasons=tuple(not_ready_reasons),
    )


def build_metal_bridge_invocation_packet(
    package: MetalKernelPackage,
    *,
    wait_until_completed: bool = False,
    allow_missing_metallib: bool = True,
    native_buffer_bindings: MetalNativeBufferBindingSet | None = None,
) -> MetalBridgeInvocationPacket:
    """Build the non-executing ABI packet for a validated bridge dylib.

    This does not call ``package.bridge_library_symbol``. It only records the
    slots the runtime will pass once metallib production and native MTLBuffer
    binding exist.
    """
    return _make_bridge_invocation_packet(
        launch_plan=package.launch_plan,
        bridge_library_path=package.bridge_library_path,
        bridge_library_symbol=package.bridge_library_symbol,
        bridge_library_load_validated=package.bridge_library_load_validated,
        wait_until_completed=wait_until_completed,
        allow_missing_metallib=allow_missing_metallib,
        native_buffer_bindings=native_buffer_bindings,
    )


def build_metal_kernel_package(
    module: KernelModule | PlainTirModule,
    packed_args: PccPackedArgs,
    artifact_dir: str | Path,
    *,
    cpu_inputs: Mapping[str, object] | None = None,
    entry: str | None = None,
    compile_metal: bool = False,
    compile_bridge: bool = False,
    link_bridge_library: bool = False,
    validate_bridge_library: bool = False,
    build_invocation_packet: bool = False,
    allow_missing_metallib: bool = True,
    wait_until_completed: bool = False,
    native_buffer_bindings: MetalNativeBufferBindingSet | None = None,
    metal_source_emitter: Callable[[PlainTirModule], str] | None = None,
    metal_source_tool: str = "pcc.kernel_ir.metal_finalize.emit_metal_source",
    bridge_compiler: Callable[..., Path] | None = None,
    bridge_linker: Callable[..., Path] | None = None,
    bridge_loader: Callable[..., str] | None = None,
    timeout: float = 30.0,
) -> MetalKernelPackage:
    """Build a non-executing package manifest for a Kernel IR Metal kernel.

    ``cpu_inputs`` is optional, but when present it must pass the CPU reference
    oracle for the current scalar tiled GEMM subset before the package is
    returned. ``compile_metal``, ``compile_bridge``, ``link_bridge_library``,
    and ``validate_bridge_library`` produce or inspect artifacts only; they
    never imply a runtime launch.
    """
    plain = _coerce_plain(module)
    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cpu_reference = None
    if cpu_inputs is not None:
        cpu_reference = execute_scalar_tiled_gemm_reference(
            plain,
            cpu_inputs,
            entry=entry,
        )

    finalize = finalize_metal(
        plain,
        artifact_dir=out_dir,
        compile_toolchain=compile_metal,
        metal_source_emitter=metal_source_emitter,
        metal_source_tool=metal_source_tool,
        timeout=timeout,
    )
    metallib_path = None
    if finalize.metallib_produced:
        metallib_path = finalize.artifact_paths.get("metallib")
        if metallib_path is None:
            raise MetalPackageError("Metal finalize claimed metallib without a path")

    launch_plan = plan_metal_launch(
        plain,
        packed_args,
        metallib_path=metallib_path,
        entry=entry,
    )
    bridge = build_metal_executor_bridge_artifacts(
        launch_plan,
        out_dir,
        compile_bridge=compile_bridge,
        compiler=bridge_compiler,
        timeout=timeout,
    )
    bridge_library_status = STATUS_BRIDGE_LIBRARY_NOT_REQUESTED
    bridge_library_path = None
    bridge_library_produced = False
    bridge_library_reason = "link_bridge_library=False; no bridge dylib was produced."
    bridge_library_symbol = None
    bridge_library_load_status = STATUS_BRIDGE_LIBRARY_LOAD_NOT_REQUESTED
    bridge_library_load_validated = False
    bridge_library_load_reason = "validate_bridge_library=False; bridge dylib was not loaded."

    if link_bridge_library:
        if not bridge.bridge_object_produced or bridge.object_path is None:
            raise MetalPackageError(
                "link_bridge_library=True requires compile_bridge=True and a bridge object artifact"
            )
        dylib_path = out_dir / f"{launch_plan.kernel_entry}_metal_bridge.dylib"
        if bridge_linker is None:
            from pcc.backend.metal import link_metal_runtime_bridge_dylib

            bridge_linker = link_metal_runtime_bridge_dylib
        from pcc.backend.metal import MetalCompileError, MetalToolchainUnavailable

        try:
            linked_path = bridge_linker(
                dylib_path,
                object_path=Path(bridge.object_path),
                timeout=timeout,
            )
        except MetalToolchainUnavailable as exc:
            bridge_library_status = STATUS_SKIPPED_WITH_REASON
            bridge_library_path = str(dylib_path)
            bridge_library_reason = (
                "Metal bridge dylib linker unavailable; bridge object was "
                f"produced but no loadable library was produced: {exc}"
            )
        except MetalCompileError as exc:
            raise MetalPackageError(f"Metal bridge dylib link failed: {exc}") from exc
        else:
            linked = Path(linked_path)
            if not linked.is_file():
                raise MetalPackageError(
                    f"Metal bridge dylib linker returned no artifact: {linked}"
                )
            bridge_library_status = STATUS_BRIDGE_LIBRARY_PRODUCED
            bridge_library_path = str(linked)
            bridge_library_produced = True
            bridge_library_reason = (
                "Host Metal executor bridge dylib artifact produced; no command "
                "buffer was committed."
            )

    if validate_bridge_library:
        if not bridge_library_produced or bridge_library_path is None:
            raise MetalPackageError(
                "validate_bridge_library=True requires link_bridge_library=True "
                "and a produced bridge dylib"
            )
        bridge_library_symbol = metal_executor_bridge_symbol(launch_plan)
        if bridge_loader is None:
            from pcc.backend.metal import validate_dynamic_library_symbol

            bridge_loader = validate_dynamic_library_symbol
        from pcc.backend.metal import MetalCompileError

        try:
            loaded_symbol = bridge_loader(
                Path(bridge_library_path),
                symbol=bridge_library_symbol,
            )
        except MetalCompileError as exc:
            raise MetalPackageError(
                f"Metal bridge dylib load/symbol validation failed: {exc}"
            ) from exc
        if loaded_symbol != bridge_library_symbol:
            raise MetalPackageError(
                f"Metal bridge dylib loader returned {loaded_symbol!r}, "
                f"expected {bridge_library_symbol!r}"
            )
        bridge_library_load_status = STATUS_BRIDGE_LIBRARY_LOAD_VALIDATED
        bridge_library_load_validated = True
        bridge_library_load_reason = (
            "Host Metal executor bridge dylib loaded and bridge symbol resolved; "
            "the bridge function was not called."
        )

    package = MetalKernelPackage(
        status="metal_kernel_package_artifacts",
        module_name=plain.module,
        artifact_dir=str(out_dir),
        cpu_reference=cpu_reference,
        finalize=finalize,
        launch_plan=launch_plan,
        bridge=bridge,
        bridge_library_status=bridge_library_status,
        bridge_library_path=bridge_library_path,
        bridge_library_produced=bridge_library_produced,
        bridge_library_reason=bridge_library_reason,
        bridge_library_symbol=bridge_library_symbol,
        bridge_library_load_status=bridge_library_load_status,
        bridge_library_load_validated=bridge_library_load_validated,
        bridge_library_load_reason=bridge_library_load_reason,
    )
    if build_invocation_packet:
        package = replace(
            package,
            bridge_invocation_packet=build_metal_bridge_invocation_packet(
                package,
                wait_until_completed=wait_until_completed,
                allow_missing_metallib=allow_missing_metallib,
                native_buffer_bindings=native_buffer_bindings,
            ),
        )
    return package


__all__ = [
    "MetalBridgeInvocationPacket",
    "MetalKernelPackage",
    "MetalPackageError",
    "STATUS_BRIDGE_LIBRARY_NOT_REQUESTED",
    "STATUS_BRIDGE_LIBRARY_PRODUCED",
    "STATUS_BRIDGE_LIBRARY_LOAD_NOT_REQUESTED",
    "STATUS_BRIDGE_LIBRARY_LOAD_VALIDATED",
    "STATUS_BRIDGE_INVOCATION_PACKET_READY",
    "STATUS_BRIDGE_INVOCATION_PACKET_NOT_READY",
    "STATUS_SKIPPED_WITH_REASON",
    "build_metal_bridge_invocation_packet",
    "build_metal_kernel_package",
    "metal_kernel_package_manifest_dict",
    "verify_metal_kernel_package_manifest",
    "write_metal_kernel_package_manifest",
]
