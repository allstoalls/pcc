"""PCC host/device target split — TargetMachine-style registry.

Row K-P0-TARGET-SPLIT. Borrows the *organization* of LLVM's ``TargetMachine``
(a capability table + a pluggable finalize pipeline), NOT its class hierarchy.

Responsibilities:
  * resolve ``host=self|c`` + ``device=metal|none`` into a resolved
    ``TargetMachine`` describing which finalize pipelines run;
  * retain self as the production execution root and reject unknown backends;
  * model the host/device split as the backend-organization root: the shared
    front-half (AST -> HIR -> Kernel IR -> plain TIR) is target-neutral; only
    the finalize back-half diverges;
  * during a host-only compile (``device=none``) NO device finalize runs.

First-slice scope: resolution + rule enforcement only. No finalize is executed
here — finalize descriptors live in metal_finalize.py.

Importable standalone::

    from pcc.kernel_ir.target_split import resolve, TargetSplitError
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class TargetSplitError(ValueError):
    """A host/device target resolution violated a target-split invariant."""


class HostBackend(enum.Enum):
    SELF = "self"
    C = "c"


class DeviceTarget(enum.Enum):
    METAL = "metal"
    NONE = "none"


@dataclass(frozen=True)
class HostCaps:
    """Capability record for a host backend (LLVM-style capability table)."""

    backend: HostBackend
    # Whether this host backend is a first-class execution root (self is).
    first_class_root: bool
    # The finalize pipeline name applied to the split-off host module.
    host_finalize: str

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend.value,
            "first_class_root": self.first_class_root,
            "host_finalize": self.host_finalize,
        }


@dataclass(frozen=True)
class DeviceCaps:
    """Capability record for a device target."""

    device: DeviceTarget
    device_finalize: str | None  # None => host-only, no device finalize runs
    async_copy: bool  # does this device model async copy / fences natively?

    def to_dict(self) -> dict[str, object]:
        return {
            "device": self.device.value,
            "device_finalize": self.device_finalize,
            "async_copy": self.async_copy,
        }


# Capability registry. This is the "TargetMachine registry": add a backend by
# registering its caps, not by hardcoding a codegen into the lowering.
_HOST_REGISTRY: dict[HostBackend, HostCaps] = {
    HostBackend.SELF: HostCaps(HostBackend.SELF, first_class_root=True, host_finalize="self_host_finalize"),
    HostBackend.C: HostCaps(HostBackend.C, first_class_root=False, host_finalize="c_host_finalize"),
}

_DEVICE_REGISTRY: dict[DeviceTarget, DeviceCaps] = {
    DeviceTarget.METAL: DeviceCaps(DeviceTarget.METAL, device_finalize="metal_device_finalize", async_copy=False),
    DeviceTarget.NONE: DeviceCaps(DeviceTarget.NONE, device_finalize=None, async_copy=False),
}

# The target-neutral shared front-half. Both host and device consume this
# identical prefix; divergence happens only after plain_tir_freeze.
SHARED_FRONT_HALF: tuple[str, ...] = (
    "hir",
    "kernel_region_extract",
    "kernel_ir",
    "tirx_lower",
    "layout_apply",
    "plain_tir_freeze",
    "split_host_device",
)


@dataclass(frozen=True)
class TargetMachine:
    """A resolved host+device target machine and its finalize plan."""

    host: HostCaps
    device: DeviceCaps
    shared_front_half: tuple[str, ...] = field(default=SHARED_FRONT_HALF)

    @property
    def runs_device_finalize(self) -> bool:
        return self.device.device_finalize is not None

    def finalize_plan(self) -> dict[str, object]:
        """The ordered finalize plan. Host finalize always runs; device
        finalize runs only when a device target is present."""
        plan: dict[str, object] = {
            "shared_front_half": list(self.shared_front_half),
            "host_finalize": self.host.host_finalize,
        }
        if self.runs_device_finalize:
            plan["device_finalize"] = self.device.device_finalize
        else:
            # Host-only compile: device finalize is explicitly absent, not a
            # silent skip. This is asserted by the test suite.
            plan["device_finalize"] = None
        return plan

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host.to_dict(),
            "device": self.device.to_dict(),
            "runs_device_finalize": self.runs_device_finalize,
            "finalize_plan": self.finalize_plan(),
        }


def _coerce_host(host: str | HostBackend) -> HostBackend:
    if isinstance(host, HostBackend):
        return host
    try:
        return HostBackend(str(host).lower())
    except ValueError as err:
        raise TargetSplitError(
            f"unknown host backend {host!r}; expected one of "
            f"{[b.value for b in HostBackend]}"
        ) from err


def _coerce_device(device: str | DeviceTarget) -> DeviceTarget:
    if isinstance(device, DeviceTarget):
        return device
    try:
        return DeviceTarget(str(device).lower())
    except ValueError as err:
        raise TargetSplitError(
            f"unknown device target {device!r}; expected one of "
            f"{[d.value for d in DeviceTarget]}"
        ) from err


def resolve(
    host: str | HostBackend = HostBackend.SELF,
    device: str | DeviceTarget = DeviceTarget.NONE,
) -> TargetMachine:
    """Resolve a host+device pair through the declared capability registry."""
    host_kind = _coerce_host(host)
    device_kind = _coerce_device(device)

    host_caps = _HOST_REGISTRY.get(host_kind)
    if host_caps is None:  # pragma: no cover - registry is exhaustive
        raise TargetSplitError(f"host backend {host_kind} is not registered")

    device_caps = _DEVICE_REGISTRY.get(device_kind)
    if device_caps is None:  # pragma: no cover - registry is exhaustive
        raise TargetSplitError(f"device target {device_kind} is not registered")

    return TargetMachine(host=host_caps, device=device_caps)


def assert_no_device_finalize_during_host_only(machine: TargetMachine) -> None:
    """Guard: a host-only (``device=none``) machine must not schedule a device
    finalize. Raises if the invariant is violated."""
    if machine.device.device == DeviceTarget.NONE and machine.runs_device_finalize:
        raise TargetSplitError(
            "host-only compile scheduled a device finalize; device finalize must "
            "not run when device=none"
        )


__all__ = [
    "TargetSplitError",
    "HostBackend",
    "DeviceTarget",
    "HostCaps",
    "DeviceCaps",
    "TargetMachine",
    "SHARED_FRONT_HALF",
    "resolve",
    "assert_no_device_finalize_during_host_only",
]
