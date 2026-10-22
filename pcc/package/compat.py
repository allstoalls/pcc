"""Package/ecosystem compatibility levels from the multi-year roadmap."""

from __future__ import annotations

from dataclasses import dataclass

LEVEL_COMPAT_PYTHON = 0
LEVEL_NOLIBPYTHON_PYTHON = 1
LEVEL_C_EXTENSION_ABI = 2
LEVEL_PCC_COMPILED_EXTENSION = 3
LEVEL_ACCELERATED_EXTENSION = 4


@dataclass(frozen=True)
class PackageTarget:
    name: str
    level: int
    summary: str
    smoke_tests: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "level": self.level,
            "summary": self.summary,
            "smoke_tests": list(self.smoke_tests),
        }


_TARGETS = {
    "pytest": PackageTarget(
        "pytest", LEVEL_COMPAT_PYTHON, "test runner compatibility target"
    ),
    "packaging": PackageTarget(
        "packaging", LEVEL_COMPAT_PYTHON, "pure-Python packaging metadata"
    ),
    "numpy": PackageTarget(
        "numpy",
        LEVEL_C_EXTENSION_ABI,
        "unchanged import via CPython C-API/extension ABI first",
    ),
    "mlx": PackageTarget(
        "mlx",
        LEVEL_C_EXTENSION_ABI,
        "Apple MLX C++/Metal extension ABI and array-runtime target; cpython-compat import first",
    ),
    "vllm": PackageTarget(
        "vllm",
        LEVEL_C_EXTENSION_ABI,
        "vLLM PyTorch/CUDA extension stack target; cpython-compat import first",
    ),
    "tilelang": PackageTarget(
        "tilelang",
        LEVEL_C_EXTENSION_ABI,
        "TileLang TVM/GPU kernel DSL compiler stack target; cpython-compat import first",
    ),
    "vllm-metal": PackageTarget(
        "vllm-metal",
        LEVEL_C_EXTENSION_ABI,
        "vLLM-Metal Apple Silicon extension stack on MLX/Metal; cpython-compat import first",
    ),
    "cffi": PackageTarget("cffi", LEVEL_C_EXTENSION_ABI, "C FFI package target"),
    "pybind11": PackageTarget(
        "pybind11", LEVEL_C_EXTENSION_ABI, "C++ extension ABI target"
    ),
    "requests": PackageTarget(
        "requests", LEVEL_NOLIBPYTHON_PYTHON, "pure-Python network stack smoke"
    ),
    "pandas": PackageTarget(
        "pandas", LEVEL_C_EXTENSION_ABI, "depends on NumPy ABI progress"
    ),
    "scipy": PackageTarget(
        "scipy", LEVEL_C_EXTENSION_ABI, "scientific extension stack audit target"
    ),
    "scikit-learn": PackageTarget(
        "scikit-learn", LEVEL_C_EXTENSION_ABI, "C/C++ extension downstream audit target"
    ),
}


def get_package_target(name: str) -> PackageTarget | None:
    return _TARGETS.get(name)


def iter_package_targets() -> tuple[PackageTarget, ...]:
    return tuple(_TARGETS[name] for name in sorted(_TARGETS))


def level_name(level: int) -> str:
    if level == LEVEL_COMPAT_PYTHON:
        return "compat_python"
    if level == LEVEL_NOLIBPYTHON_PYTHON:
        return "nolibpython_python"
    if level == LEVEL_C_EXTENSION_ABI:
        return "c_extension_abi"
    if level == LEVEL_PCC_COMPILED_EXTENSION:
        return "pcc_compiled_extension"
    if level == LEVEL_ACCELERATED_EXTENSION:
        return "accelerated_extension"
    raise ValueError(f"unknown package compatibility level {level}")
