from __future__ import annotations

"""User-facing markers for pcc GPU kernels.

These objects are compile-time markers.  Host CPython can import them so source
files remain inspectable, but pcc lowers ``@gpu.kernel`` functions only when a
device backend such as ``--gpu-backend=metal`` is selected.
"""


class _GpuType:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return "gpu." + self.name


ptr_f32 = _GpuType("ptr_f32")
i32 = _GpuType("i32")
u32 = _GpuType("u32")
f32 = _GpuType("f32")


def kernel(fn):
    fn.__pcc_kernel_ir_entry__ = True
    return fn


def thread_id_x() -> int:
    raise RuntimeError("pcc.gpu.thread_id_x() is only valid inside a GPU kernel")


def run_add_f32_demo() -> int:
    raise RuntimeError(
        "pcc.gpu.run_add_f32_demo() must be lowered by pcc --gpu-backend=metal"
    )
