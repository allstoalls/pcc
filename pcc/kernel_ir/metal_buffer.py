"""Native Metal buffer binding records for Kernel IR launches.

This module sits between ``PccBufferHandle`` and the Objective-C executor
bridge. A ``PccBufferHandle.handle_id`` is a logical pcc handle, not a native
``id<MTLBuffer>`` pointer. Only an explicit ``MetalNativeBufferBindingSet`` can
mark a launch packet's buffer slots as native-buffer-ready.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pcc.kernel_ir.metal_launch import MetalLaunchPlan

STATUS_NATIVE_BUFFER_BINDINGS_READY = "metal_native_buffer_bindings_ready"
STATUS_NATIVE_BUFFER_RUNTIME_SOURCE_ONLY = "metal_native_buffer_runtime_source_only"
STATUS_NATIVE_BUFFER_RUNTIME_OBJECT_PRODUCED = "metal_native_buffer_runtime_object_produced"
STATUS_NATIVE_BUFFER_RUNTIME_LIBRARY_PRODUCED = "metal_native_buffer_runtime_library_produced"
STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED = "metal_native_buffer_runtime_load_validated"
STATUS_NATIVE_BUFFER_RUNTIME_CALL_VALIDATED = "metal_native_buffer_runtime_call_validated"
STATUS_NATIVE_BUFFER_ALLOCATIONS_READY = "metal_native_buffer_allocations_ready"
STATUS_NATIVE_BUFFER_DATA_TRANSFER_VALIDATED = "metal_native_buffer_data_transfer_validated"
STATUS_NATIVE_BUFFER_DATA_ROUNDTRIP_VALIDATED = "metal_native_buffer_data_roundtrip_validated"
STATUS_SKIPPED_WITH_REASON = "SKIPPED_WITH_REASON"

_CREATE_SYMBOL = "pcc_metal_buffer_runtime_create"
_LENGTH_SYMBOL = "pcc_metal_buffer_runtime_length"
_RELEASE_SYMBOL = "pcc_metal_buffer_runtime_release"
_WRITE_SYMBOL = "pcc_metal_buffer_runtime_write"
_READ_SYMBOL = "pcc_metal_buffer_runtime_read"
_RUNTIME_SYMBOLS = (
    _CREATE_SYMBOL,
    _LENGTH_SYMBOL,
    _RELEASE_SYMBOL,
    _WRITE_SYMBOL,
    _READ_SYMBOL,
)


class MetalNativeBufferBindingError(ValueError):
    """Native Metal buffer bindings did not match a launch plan."""


class MetalNativeBufferRuntimeError(ValueError):
    """The native Metal buffer runtime bridge failed."""


@dataclass(frozen=True)
class MetalNativeBufferBinding:
    """One runtime-supplied native ``id<MTLBuffer>`` binding."""

    name: str
    kernel_index: int
    bridge_ordinal: int
    handle_id: int
    dtype: str
    native_mtlbuffer_ptr: int
    source: str
    shape: tuple[int, ...] | None = None
    required_nbytes: int | None = None
    provided_nbytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "kernel_index": self.kernel_index,
            "bridge_ordinal": self.bridge_ordinal,
            "handle_id": self.handle_id,
            "dtype": self.dtype,
            "native_mtlbuffer_ptr": self.native_mtlbuffer_ptr,
            "native_mtlbuffer_bound": True,
            "source": self.source,
        }
        if self.shape is not None:
            data["shape"] = list(self.shape)
        if self.required_nbytes is not None:
            data["required_nbytes"] = self.required_nbytes
        if self.provided_nbytes is not None:
            data["provided_nbytes"] = self.provided_nbytes
        return data


@dataclass(frozen=True)
class MetalNativeBufferBindingSet:
    """Native buffer bindings for every buffer argument in one launch plan."""

    status: str
    bindings: tuple[MetalNativeBufferBinding, ...]
    native_buffer_handles_ready: bool = True
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False
    claim_mode: str = "Metal native buffer bindings, not launched"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "claim_mode": self.claim_mode,
            "bindings": [binding.to_dict() for binding in self.bindings],
            "native_buffer_handles_ready": self.native_buffer_handles_ready,
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
        }


@dataclass(frozen=True)
class MetalNativeBufferRuntimeArtifacts:
    """Artifact state for the native MTLBuffer allocator bridge."""

    status: str
    source_path: str
    source: str
    object_path: str | None = None
    library_path: str | None = None
    validated_symbols: tuple[str, ...] = ()
    reason: str = ""
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_path": self.source_path,
            "object_path": self.object_path,
            "library_path": self.library_path,
            "validated_symbols": list(self.validated_symbols),
            "reason": self.reason,
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
        }


@dataclass(frozen=True)
class MetalNativeBufferRuntimeSmokeResult:
    """Result of creating and releasing one native MTLBuffer."""

    status: str
    nbytes_requested: int
    nbytes_reported: int | None
    native_mtlbuffer_ptr: int | None
    released: bool
    reason: str
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "nbytes_requested": self.nbytes_requested,
            "nbytes_reported": self.nbytes_reported,
            "native_mtlbuffer_ptr": self.native_mtlbuffer_ptr,
            "released": self.released,
            "reason": self.reason,
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
        }


@dataclass(frozen=True)
class MetalNativeBufferDataTransferResult:
    """Result of copying bytes between host memory and one native MTLBuffer."""

    status: str
    direction: str
    native_mtlbuffer_ptr: int
    offset: int
    nbytes: int
    data: bytes | None = None
    reason: str = ""
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "direction": self.direction,
            "native_mtlbuffer_ptr": self.native_mtlbuffer_ptr,
            "offset": self.offset,
            "nbytes": self.nbytes,
            "data_hex": self.data.hex() if self.data is not None else None,
            "reason": self.reason,
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
        }


@dataclass(frozen=True)
class MetalNativeBufferAllocation:
    """One native MTLBuffer allocated for a launch-plan buffer arg."""

    name: str
    handle_id: int
    native_mtlbuffer_ptr: int
    requested_nbytes: int
    reported_nbytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "handle_id": self.handle_id,
            "native_mtlbuffer_ptr": self.native_mtlbuffer_ptr,
            "requested_nbytes": self.requested_nbytes,
            "reported_nbytes": self.reported_nbytes,
        }


@dataclass
class MetalNativeBufferAllocationSet:
    """Owned native MTLBuffer allocations plus launch-plan bindings."""

    status: str
    allocations: tuple[MetalNativeBufferAllocation, ...]
    binding_set: MetalNativeBufferBindingSet | None
    reason: str
    released: bool = False
    runtime_launch_executed: bool = False
    whole_program_gpu: bool = False
    _release_fn: Callable[[ctypes.c_void_p], int] | None = field(default=None, repr=False)
    _released_handle_ids: set[int] = field(default_factory=set, repr=False)

    def release_handle(self, handle_id: int) -> None:
        """Release one native MTLBuffer by logical pcc handle id.

        DLPack-style tensor deleters operate at tensor granularity, so the
        allocation set must not force a whole-launch release when only one
        buffer's final alias dies. ``release_all`` remains idempotent and skips
        handles already released here.
        """
        allocation = next(
            (candidate for candidate in self.allocations if candidate.handle_id == handle_id),
            None,
        )
        if allocation is None:
            raise MetalNativeBufferRuntimeError(
                f"native MTLBuffer allocation not found for handle {handle_id}"
            )
        if handle_id in self._released_handle_ids:
            return
        if self._release_fn is not None:
            rc = int(self._release_fn(ctypes.c_void_p(allocation.native_mtlbuffer_ptr)))
            if rc != 0:
                raise MetalNativeBufferRuntimeError(
                    f"native MTLBuffer release failed for handle {handle_id} with rc={rc}"
                )
        self._released_handle_ids.add(handle_id)
        if len(self._released_handle_ids) == len(self.allocations):
            self.released = True

    def release_all(self) -> None:
        if self.released:
            return
        for allocation in self.allocations:
            if allocation.handle_id in self._released_handle_ids:
                continue
            self.release_handle(allocation.handle_id)
        self.released = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "binding_set": self.binding_set.to_dict() if self.binding_set else None,
            "reason": self.reason,
            "released": self.released,
            "released_handle_ids": sorted(self._released_handle_ids),
            "runtime_launch_executed": self.runtime_launch_executed,
            "whole_program_gpu": self.whole_program_gpu,
        }


def build_metal_native_buffer_binding_set(
    launch_plan: MetalLaunchPlan,
    native_mtlbuffer_ptrs: Mapping[int, int],
    *,
    source: str = "runtime-supplied id<MTLBuffer> pointer",
) -> MetalNativeBufferBindingSet:
    """Validate native ``id<MTLBuffer>`` pointers for a launch plan.

    The mapping key is the logical ``PccBufferHandle.handle_id`` recorded in the
    launch plan. The mapping value must be a non-zero integer representation of
    a runtime-owned native ``id<MTLBuffer>`` pointer. This function does not
    allocate Metal buffers and does not submit work; it only proves that the
    bridge packet has a distinct native-pointer binding for every buffer slot.
    """
    buffer_args = [arg for arg in launch_plan.args if arg.kind == "buffer"]
    expected_ids: set[int] = set()
    for arg in buffer_args:
        if arg.handle_id is None:
            raise MetalNativeBufferBindingError(
                f"buffer argument {arg.name!r} has no PccBufferHandle id"
            )
        expected_ids.add(arg.handle_id)

    provided_ids = set(native_mtlbuffer_ptrs)
    missing = expected_ids - provided_ids
    extra = provided_ids - expected_ids
    if missing:
        raise MetalNativeBufferBindingError(
            f"missing native MTLBuffer pointer for handle ids {sorted(missing)}"
        )
    if extra:
        raise MetalNativeBufferBindingError(
            f"native MTLBuffer pointer supplied for non-launch handle ids {sorted(extra)}"
        )

    bindings: list[MetalNativeBufferBinding] = []
    for ordinal, arg in enumerate(buffer_args):
        assert arg.handle_id is not None
        ptr = native_mtlbuffer_ptrs[arg.handle_id]
        if not isinstance(ptr, int) or ptr <= 0:
            raise MetalNativeBufferBindingError(
                f"native MTLBuffer pointer for handle {arg.handle_id} must be a non-zero int"
            )
        bindings.append(
            MetalNativeBufferBinding(
                name=arg.name,
                kernel_index=arg.index,
                bridge_ordinal=ordinal,
                handle_id=arg.handle_id,
                dtype=arg.dtype,
                native_mtlbuffer_ptr=ptr,
                source=source,
                shape=arg.shape,
                required_nbytes=arg.required_nbytes,
                provided_nbytes=arg.provided_nbytes,
            )
        )

    return MetalNativeBufferBindingSet(
        status=STATUS_NATIVE_BUFFER_BINDINGS_READY,
        bindings=tuple(bindings),
    )


def _load_native_buffer_runtime_functions(
    library_path: str | Path,
    *,
    cdll_factory: Callable[[str], Any] | None = None,
) -> tuple[Any, Any, Any, Any, Any]:
    lib_path = Path(library_path)
    if not lib_path.is_file():
        raise MetalNativeBufferRuntimeError(f"native buffer runtime library not found: {lib_path}")
    try:
        load_library = cdll_factory if cdll_factory is not None else ctypes.CDLL
        lib = load_library(str(lib_path))
    except OSError as exc:
        raise MetalNativeBufferRuntimeError(
            f"native buffer runtime library load failed: {exc}"
        ) from exc

    create = getattr(lib, _CREATE_SYMBOL)
    length = getattr(lib, _LENGTH_SYMBOL)
    release = getattr(lib, _RELEASE_SYMBOL)
    write = getattr(lib, _WRITE_SYMBOL)
    read = getattr(lib, _READ_SYMBOL)
    create.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_int64
    length.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
    length.restype = ctypes.c_int64
    release.argtypes = [ctypes.c_void_p]
    release.restype = ctypes.c_int64
    write.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64]
    write.restype = ctypes.c_int64
    read.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64]
    read.restype = ctypes.c_int64
    return create, length, release, write, read


def _buffer_allocation_nbytes(arg: Any) -> int:
    nbytes = arg.provided_nbytes if arg.provided_nbytes is not None else arg.required_nbytes
    if not isinstance(nbytes, int) or nbytes <= 0:
        raise MetalNativeBufferRuntimeError(
            f"buffer argument {arg.name!r} has no positive byte size for native allocation"
        )
    return nbytes


def allocate_metal_native_buffers_for_plan(
    library_path: str | Path,
    launch_plan: MetalLaunchPlan,
    *,
    cdll_factory: Callable[[str], Any] | None = None,
) -> MetalNativeBufferAllocationSet:
    """Allocate native MTLBuffers for every buffer arg in *launch_plan*.

    The returned allocation set owns the native buffers until ``release_all`` is
    called. No command queue, command buffer, or GPU dispatch is created here.
    """
    create, length, release, _, _ = _load_native_buffer_runtime_functions(
        library_path,
        cdll_factory=cdll_factory,
    )
    buffer_args = [arg for arg in launch_plan.args if arg.kind == "buffer"]
    allocations: list[MetalNativeBufferAllocation] = []
    native_ptrs: dict[int, int] = {}
    try:
        for arg in buffer_args:
            if arg.handle_id is None:
                raise MetalNativeBufferRuntimeError(
                    f"buffer argument {arg.name!r} has no PccBufferHandle id"
                )
            if arg.handle_id in native_ptrs:
                raise MetalNativeBufferRuntimeError(
                    f"duplicate buffer handle id in launch plan: {arg.handle_id}"
                )
            requested_nbytes = _buffer_allocation_nbytes(arg)
            native_buffer = ctypes.c_void_p()
            create_rc = int(
                create(ctypes.c_uint64(requested_nbytes), ctypes.byref(native_buffer))
            )
            if create_rc == 3:
                for allocation in reversed(allocations):
                    release(ctypes.c_void_p(allocation.native_mtlbuffer_ptr))
                return MetalNativeBufferAllocationSet(
                    status=STATUS_SKIPPED_WITH_REASON,
                    allocations=tuple(),
                    binding_set=None,
                    reason="MTLCreateSystemDefaultDevice returned nil; no Metal device available.",
                )
            if create_rc != 0 or not native_buffer.value:
                raise MetalNativeBufferRuntimeError(
                    f"native MTLBuffer create failed for {arg.name!r} with rc={create_rc}"
                )
            ptr_value = int(native_buffer.value)
            try:
                reported = ctypes.c_uint64()
                length_rc = int(length(native_buffer, ctypes.byref(reported)))
                if length_rc != 0:
                    raise MetalNativeBufferRuntimeError(
                        f"native MTLBuffer length failed for {arg.name!r} with rc={length_rc}"
                    )
                reported_nbytes = int(reported.value)
                if reported_nbytes < requested_nbytes:
                    raise MetalNativeBufferRuntimeError(
                        f"native MTLBuffer for {arg.name!r} reports {reported_nbytes} bytes, "
                        f"expected at least {requested_nbytes}"
                    )
            except Exception:
                release(ctypes.c_void_p(ptr_value))
                raise
            native_ptrs[arg.handle_id] = ptr_value
            allocations.append(
                MetalNativeBufferAllocation(
                    name=arg.name,
                    handle_id=arg.handle_id,
                    native_mtlbuffer_ptr=ptr_value,
                    requested_nbytes=requested_nbytes,
                    reported_nbytes=reported_nbytes,
                )
            )
    except Exception:
        for allocation in reversed(allocations):
            release(ctypes.c_void_p(allocation.native_mtlbuffer_ptr))
        raise

    binding_set = build_metal_native_buffer_binding_set(
        launch_plan,
        native_ptrs,
        source="pcc native Metal buffer runtime allocation",
    )
    return MetalNativeBufferAllocationSet(
        status=STATUS_NATIVE_BUFFER_ALLOCATIONS_READY,
        allocations=tuple(allocations),
        binding_set=binding_set,
        reason="Native MTLBuffers allocated and bound to the launch plan; not launched.",
        _release_fn=release,
    )


def emit_metal_native_buffer_runtime_source() -> str:
    """Emit Objective-C C ABI helpers for native MTLBuffer allocation.

    The bridge exposes allocation, length inspection, host byte write/read, and
    release only. It does not create command queues, command buffers, encoders,
    or dispatch work.
    """
    return """#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdint.h>
#include <string.h>

int64_t pcc_metal_buffer_runtime_create(uint64_t nbytes, void **out_buffer) {
  @autoreleasepool {
    if (out_buffer == NULL) { return 2; }
    *out_buffer = NULL;
    if (nbytes == 0) { return 2; }
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (device == nil) { return 3; }
    id<MTLBuffer> buffer = [device newBufferWithLength:(NSUInteger)nbytes
                                               options:MTLResourceStorageModeShared];
    if (buffer == nil) { return 4; }
    *out_buffer = (__bridge_retained void *)buffer;
    return 0;
  }
}

int64_t pcc_metal_buffer_runtime_length(void *buffer, uint64_t *out_nbytes) {
  @autoreleasepool {
    if (buffer == NULL || out_nbytes == NULL) { return 2; }
    id<MTLBuffer> mtl_buffer = (__bridge id<MTLBuffer>)buffer;
    *out_nbytes = (uint64_t)[mtl_buffer length];
    return 0;
  }
}

int64_t pcc_metal_buffer_runtime_write(
    void *buffer, uint64_t offset, const void *src, uint64_t nbytes) {
  @autoreleasepool {
    if (buffer == NULL || src == NULL) { return 2; }
    id<MTLBuffer> mtl_buffer = (__bridge id<MTLBuffer>)buffer;
    uint64_t length = (uint64_t)[mtl_buffer length];
    if (offset > length || nbytes > length - offset) { return 3; }
    void *contents = [mtl_buffer contents];
    if (contents == NULL) { return 4; }
    memcpy((uint8_t *)contents + offset, src, (size_t)nbytes);
    return 0;
  }
}

int64_t pcc_metal_buffer_runtime_read(
    void *buffer, uint64_t offset, void *dst, uint64_t nbytes) {
  @autoreleasepool {
    if (buffer == NULL || dst == NULL) { return 2; }
    id<MTLBuffer> mtl_buffer = (__bridge id<MTLBuffer>)buffer;
    uint64_t length = (uint64_t)[mtl_buffer length];
    if (offset > length || nbytes > length - offset) { return 3; }
    void *contents = [mtl_buffer contents];
    if (contents == NULL) { return 4; }
    memcpy(dst, (uint8_t *)contents + offset, (size_t)nbytes);
    return 0;
  }
}

int64_t pcc_metal_buffer_runtime_release(void *buffer) {
  @autoreleasepool {
    if (buffer == NULL) { return 2; }
    id obj = (__bridge_transfer id)buffer;
    (void)obj;
    return 0;
  }
}
"""


def build_metal_native_buffer_runtime_artifacts(
    artifact_dir: str | Path,
    *,
    compile_runtime: bool = False,
    link_runtime_library: bool = False,
    validate_symbols: bool = False,
    compiler: Callable[..., Path] | None = None,
    linker: Callable[..., Path] | None = None,
    loader: Callable[..., str] | None = None,
    timeout: float = 30.0,
) -> MetalNativeBufferRuntimeArtifacts:
    """Write and optionally build/load the native MTLBuffer runtime bridge."""
    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = emit_metal_native_buffer_runtime_source()
    source_path = out_dir / "pcc_metal_buffer_runtime.m"
    source_path.write_text(source, encoding="utf-8")
    object_path = source_path.with_suffix(".o")
    library_path = out_dir / "pcc_metal_buffer_runtime.dylib"

    if not compile_runtime:
        return MetalNativeBufferRuntimeArtifacts(
            status=STATUS_NATIVE_BUFFER_RUNTIME_SOURCE_ONLY,
            source_path=str(source_path),
            source=source,
            reason="Native MTLBuffer runtime source written; no object or library was produced.",
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
        return MetalNativeBufferRuntimeArtifacts(
            status=STATUS_SKIPPED_WITH_REASON,
            source_path=str(source_path),
            source=source,
            object_path=str(object_path),
            reason=f"Native MTLBuffer runtime compiler unavailable: {exc}",
        )
    except MetalCompileError as exc:
        raise MetalNativeBufferRuntimeError(
            f"native MTLBuffer runtime source failed to compile: {exc}"
        ) from exc
    compiled = Path(compiled_path)
    if not compiled.is_file():
        raise MetalNativeBufferRuntimeError(
            f"native MTLBuffer runtime compiler returned no object: {compiled}"
        )

    if not link_runtime_library:
        return MetalNativeBufferRuntimeArtifacts(
            status=STATUS_NATIVE_BUFFER_RUNTIME_OBJECT_PRODUCED,
            source_path=str(source_path),
            source=source,
            object_path=str(compiled),
            reason="Native MTLBuffer runtime object produced; no library was linked.",
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
        return MetalNativeBufferRuntimeArtifacts(
            status=STATUS_SKIPPED_WITH_REASON,
            source_path=str(source_path),
            source=source,
            object_path=str(compiled),
            library_path=str(library_path),
            reason=f"Native MTLBuffer runtime linker unavailable: {exc}",
        )
    except MetalCompileError as exc:
        raise MetalNativeBufferRuntimeError(
            f"native MTLBuffer runtime library failed to link: {exc}"
        ) from exc
    linked = Path(linked_path)
    if not linked.is_file():
        raise MetalNativeBufferRuntimeError(
            f"native MTLBuffer runtime linker returned no library: {linked}"
        )

    if not validate_symbols:
        return MetalNativeBufferRuntimeArtifacts(
            status=STATUS_NATIVE_BUFFER_RUNTIME_LIBRARY_PRODUCED,
            source_path=str(source_path),
            source=source,
            object_path=str(compiled),
            library_path=str(linked),
            reason="Native MTLBuffer runtime library produced; symbols were not loaded.",
        )

    if loader is None:
        from pcc.backend.metal import validate_dynamic_library_symbol

        loader = validate_dynamic_library_symbol
    validated: list[str] = []
    for symbol in _RUNTIME_SYMBOLS:
        try:
            loaded_symbol = loader(linked, symbol=symbol)
        except MetalCompileError as exc:
            raise MetalNativeBufferRuntimeError(
                f"native MTLBuffer runtime symbol validation failed: {exc}"
            ) from exc
        if loaded_symbol != symbol:
            raise MetalNativeBufferRuntimeError(
                f"native MTLBuffer runtime loader returned {loaded_symbol!r}, expected {symbol!r}"
            )
        validated.append(symbol)

    return MetalNativeBufferRuntimeArtifacts(
        status=STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED,
        source_path=str(source_path),
        source=source,
        object_path=str(compiled),
        library_path=str(linked),
        validated_symbols=tuple(validated),
        reason="Native MTLBuffer runtime library loaded and required symbols resolved.",
    )


def smoke_metal_native_buffer_runtime(
    library_path: str | Path,
    *,
    nbytes: int = 64,
    cdll_factory: Callable[[str], Any] | None = None,
) -> MetalNativeBufferRuntimeSmokeResult:
    """Create, inspect, and release one native MTLBuffer through the bridge."""
    if nbytes <= 0:
        raise MetalNativeBufferRuntimeError("smoke nbytes must be positive")
    create, length, release, _, _ = _load_native_buffer_runtime_functions(
        library_path,
        cdll_factory=cdll_factory,
    )

    native_buffer = ctypes.c_void_p()
    create_rc = int(create(ctypes.c_uint64(nbytes), ctypes.byref(native_buffer)))
    if create_rc == 3:
        return MetalNativeBufferRuntimeSmokeResult(
            status=STATUS_SKIPPED_WITH_REASON,
            nbytes_requested=nbytes,
            nbytes_reported=None,
            native_mtlbuffer_ptr=None,
            released=False,
            reason="MTLCreateSystemDefaultDevice returned nil; no Metal device available.",
        )
    if create_rc != 0 or not native_buffer.value:
        raise MetalNativeBufferRuntimeError(
            f"native MTLBuffer create failed with rc={create_rc}"
        )

    ptr_value = int(native_buffer.value)
    reported_value: int | None = None
    try:
        reported = ctypes.c_uint64()
        length_rc = int(length(native_buffer, ctypes.byref(reported)))
        if length_rc != 0:
            raise MetalNativeBufferRuntimeError(
                f"native MTLBuffer length failed with rc={length_rc}"
            )
        reported_value = int(reported.value)
    finally:
        release_rc = int(release(native_buffer))
        if release_rc != 0:
            raise MetalNativeBufferRuntimeError(
                f"native MTLBuffer release failed with rc={release_rc}"
            )
    return MetalNativeBufferRuntimeSmokeResult(
        status=STATUS_NATIVE_BUFFER_RUNTIME_CALL_VALIDATED,
        nbytes_requested=nbytes,
        nbytes_reported=reported_value,
        native_mtlbuffer_ptr=ptr_value,
        released=True,
        reason="Native MTLBuffer created, length inspected, and released.",
    )


def _validate_transfer_args(native_mtlbuffer_ptr: int, nbytes: int, offset: int) -> None:
    if not isinstance(native_mtlbuffer_ptr, int) or native_mtlbuffer_ptr <= 0:
        raise MetalNativeBufferRuntimeError("native MTLBuffer pointer must be a non-zero int")
    if not isinstance(offset, int) or offset < 0:
        raise MetalNativeBufferRuntimeError("native MTLBuffer transfer offset must be >= 0")
    if not isinstance(nbytes, int) or nbytes <= 0:
        raise MetalNativeBufferRuntimeError("native MTLBuffer transfer nbytes must be > 0")


def write_metal_native_buffer(
    library_path: str | Path,
    native_mtlbuffer_ptr: int,
    data: bytes | bytearray | memoryview,
    *,
    offset: int = 0,
    cdll_factory: Callable[[str], Any] | None = None,
) -> MetalNativeBufferDataTransferResult:
    """Copy host bytes into a native MTLBuffer without launching work."""
    payload = bytes(data)
    _validate_transfer_args(native_mtlbuffer_ptr, len(payload), offset)
    _, _, _, write, _ = _load_native_buffer_runtime_functions(
        library_path,
        cdll_factory=cdll_factory,
    )
    src = ctypes.create_string_buffer(payload)
    rc = int(
        write(
            ctypes.c_void_p(native_mtlbuffer_ptr),
            ctypes.c_uint64(offset),
            ctypes.cast(src, ctypes.c_void_p),
            ctypes.c_uint64(len(payload)),
        )
    )
    if rc != 0:
        raise MetalNativeBufferRuntimeError(
            f"native MTLBuffer host write failed with rc={rc}"
        )
    return MetalNativeBufferDataTransferResult(
        status=STATUS_NATIVE_BUFFER_DATA_TRANSFER_VALIDATED,
        direction="host_to_mtlbuffer",
        native_mtlbuffer_ptr=native_mtlbuffer_ptr,
        offset=offset,
        nbytes=len(payload),
        reason="Host bytes copied into native MTLBuffer; no command buffer submitted.",
    )


def read_metal_native_buffer(
    library_path: str | Path,
    native_mtlbuffer_ptr: int,
    nbytes: int,
    *,
    offset: int = 0,
    cdll_factory: Callable[[str], Any] | None = None,
) -> MetalNativeBufferDataTransferResult:
    """Copy bytes from a native MTLBuffer into host memory without launching work."""
    _validate_transfer_args(native_mtlbuffer_ptr, nbytes, offset)
    _, _, _, _, read = _load_native_buffer_runtime_functions(
        library_path,
        cdll_factory=cdll_factory,
    )
    dst = ctypes.create_string_buffer(nbytes)
    rc = int(
        read(
            ctypes.c_void_p(native_mtlbuffer_ptr),
            ctypes.c_uint64(offset),
            ctypes.cast(dst, ctypes.c_void_p),
            ctypes.c_uint64(nbytes),
        )
    )
    if rc != 0:
        raise MetalNativeBufferRuntimeError(
            f"native MTLBuffer host read failed with rc={rc}"
        )
    return MetalNativeBufferDataTransferResult(
        status=STATUS_NATIVE_BUFFER_DATA_TRANSFER_VALIDATED,
        direction="mtlbuffer_to_host",
        native_mtlbuffer_ptr=native_mtlbuffer_ptr,
        offset=offset,
        nbytes=nbytes,
        data=bytes(dst.raw),
        reason="Native MTLBuffer bytes copied back to host; no command buffer submitted.",
    )


def roundtrip_metal_native_buffer_bytes(
    library_path: str | Path,
    native_mtlbuffer_ptr: int,
    data: bytes | bytearray | memoryview,
    *,
    offset: int = 0,
    cdll_factory: Callable[[str], Any] | None = None,
) -> MetalNativeBufferDataTransferResult:
    """Write bytes to a native MTLBuffer and read them back as a data-path proof."""
    payload = bytes(data)
    write_metal_native_buffer(
        library_path,
        native_mtlbuffer_ptr,
        payload,
        offset=offset,
        cdll_factory=cdll_factory,
    )
    read_result = read_metal_native_buffer(
        library_path,
        native_mtlbuffer_ptr,
        len(payload),
        offset=offset,
        cdll_factory=cdll_factory,
    )
    if read_result.data != payload:
        raise MetalNativeBufferRuntimeError("native MTLBuffer byte roundtrip mismatch")
    return MetalNativeBufferDataTransferResult(
        status=STATUS_NATIVE_BUFFER_DATA_ROUNDTRIP_VALIDATED,
        direction="host_mtlbuffer_host",
        native_mtlbuffer_ptr=native_mtlbuffer_ptr,
        offset=offset,
        nbytes=len(payload),
        data=payload,
        reason="Host bytes round-tripped through native MTLBuffer; no command buffer submitted.",
    )


__all__ = [
    "MetalNativeBufferBinding",
    "MetalNativeBufferBindingError",
    "MetalNativeBufferBindingSet",
    "MetalNativeBufferAllocation",
    "MetalNativeBufferAllocationSet",
    "MetalNativeBufferRuntimeArtifacts",
    "MetalNativeBufferDataTransferResult",
    "MetalNativeBufferRuntimeError",
    "MetalNativeBufferRuntimeSmokeResult",
    "STATUS_NATIVE_BUFFER_ALLOCATIONS_READY",
    "STATUS_NATIVE_BUFFER_BINDINGS_READY",
    "STATUS_NATIVE_BUFFER_DATA_ROUNDTRIP_VALIDATED",
    "STATUS_NATIVE_BUFFER_DATA_TRANSFER_VALIDATED",
    "STATUS_NATIVE_BUFFER_RUNTIME_CALL_VALIDATED",
    "STATUS_NATIVE_BUFFER_RUNTIME_LIBRARY_PRODUCED",
    "STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED",
    "STATUS_NATIVE_BUFFER_RUNTIME_OBJECT_PRODUCED",
    "STATUS_NATIVE_BUFFER_RUNTIME_SOURCE_ONLY",
    "STATUS_SKIPPED_WITH_REASON",
    "allocate_metal_native_buffers_for_plan",
    "build_metal_native_buffer_binding_set",
    "build_metal_native_buffer_runtime_artifacts",
    "emit_metal_native_buffer_runtime_source",
    "read_metal_native_buffer",
    "roundtrip_metal_native_buffer_bytes",
    "smoke_metal_native_buffer_runtime",
    "write_metal_native_buffer",
]
