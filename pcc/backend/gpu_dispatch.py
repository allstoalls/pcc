from __future__ import annotations

"""Device backend selection for annotated GPU work.

This is intentionally separate from ``pcc.backend``: the normal backend still
owns CPU host code.  A GPU backend only describes where explicitly marked device
kernels may be lowered once the kernel DSL exists.
"""

from dataclasses import dataclass
import os


_ENV_GPU_BACKEND = "PCC_GPU_BACKEND"
_DEFAULT_GPU_BACKEND = "none"


@dataclass(frozen=True)
class GpuBackendConfig:
    kind: str
    semver: str
    supported: bool
    capabilities: tuple[str, ...]
    requested: str | None = None

    def cache_signature(self) -> str:
        return f"{self.kind}:{self.semver}:" + (
            "support" if self.supported else "unsupported"
        )

    def capabilities_csv(self) -> str:
        return ",".join(self.capabilities) if self.capabilities else ""


_GPU_BACKEND_TABLE = {
    "none": {
        "semver": "host-only",
        "supported": True,
        "capabilities": (),
    },
    "metal": {
        "semver": "metal-device-config-v0",
        "supported": True,
        "capabilities": (
            "host-device-split",
            "metal-shading-language",
            "metal-toolchain-probe",
            "embedded-metallib",
            "sidecar-metallib-fallback",
            "demo-host-launch",
            "annotated-kernel-only",
        ),
    },
}


def _normalize_gpu_backend_name(value: str | None) -> str:
    if not value:
        return _DEFAULT_GPU_BACKEND
    candidate = value.strip().lower()
    if not candidate:
        return _DEFAULT_GPU_BACKEND
    if candidate in ("off", "disabled", "host", "cpu"):
        return "none"
    return candidate


def resolve_gpu_backend(requested: str | None = None) -> GpuBackendConfig:
    env_raw = os.environ.get(_ENV_GPU_BACKEND)
    kind = _normalize_gpu_backend_name(
        requested if requested is not None else env_raw
    )

    if kind not in _GPU_BACKEND_TABLE:
        known = ", ".join(sorted(_GPU_BACKEND_TABLE))
        raise ValueError(
            f"unknown gpu backend {kind!r}; expected one of: {known}"
        )

    info = _GPU_BACKEND_TABLE[kind]
    return GpuBackendConfig(
        kind=kind,
        semver=info["semver"],
        supported=bool(info["supported"]),
        capabilities=tuple(info["capabilities"]),
        requested=None
        if requested is None
        else _normalize_gpu_backend_name(requested),
    )


def gpu_backend_signature(config: GpuBackendConfig | str | None) -> str:
    if isinstance(config, str):
        return resolve_gpu_backend(config).cache_signature()
    if config is None:
        return resolve_gpu_backend(None).cache_signature()
    return config.cache_signature()


def gpu_backend_env_name() -> str:
    return _ENV_GPU_BACKEND


def all_gpu_backend_names() -> tuple[str, ...]:
    return tuple(sorted(_GPU_BACKEND_TABLE))


def gpu_backend_capabilities(
    config: GpuBackendConfig | str | None,
) -> tuple[str, ...]:
    if isinstance(config, str):
        return resolve_gpu_backend(config).capabilities
    if config is None:
        return resolve_gpu_backend(None).capabilities
    return config.capabilities
