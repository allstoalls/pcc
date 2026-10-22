import pytest
import subprocess
import sys

from pcc.kernel_ir.entry import (
    GpuKernelError,
    lower_function_to_kernel_ir,
    lower_function_to_metal,
    prepare_gpu_kernels_for_source,
    strip_gpu_kernel_host_source,
)
from pcc.backend.metal import (
    MetalCompileError,
    MetalToolchainUnavailable,
    compile_metal_source_to_air,
    find_metal_compiler,
)


MINIMAL_METAL_KERNEL = """\
#include <metal_stdlib>
using namespace metal;

kernel void pcc_empty(
    device float* out [[buffer(0)]],
    uint gid [[thread_position_in_grid]]
) {
    out[gid] = out[gid];
}
"""


PCC_GPU_VECTOR_ADD = """\
from pcc import gpu

pytestmark = pytest.mark.pcc_gate(probe="metal")


@gpu.kernel
def add(a: gpu.ptr_f32, b: gpu.ptr_f32, out: gpu.ptr_f32, n: gpu.u32):
    i = gpu.thread_id_x()
    if i < n:
        out[i] = a[i] + b[i]

def main():
    print(0)
"""


PCC_GPU_VECTOR_ADD_LAUNCH = """\
from pcc import gpu

@gpu.kernel
def add(a: gpu.ptr_f32, b: gpu.ptr_f32, out: gpu.ptr_f32, n: gpu.u32):
    i = gpu.thread_id_x()
    if i < n:
        out[i] = a[i] + b[i]

def main():
    rc: int = gpu.run_add_f32_demo()
    print(rc)

main()
"""


PCC_GPU_STRUCTURED_SAXPY = """\
from pcc import gpu

@gpu.kernel
def saxpy(a: gpu.ptr_f32, b: gpu.ptr_f32, out: gpu.ptr_f32, n: gpu.u32, alpha: gpu.f32):
    i = gpu.thread_id_x()
    if i < n:
        value = a[i] * alpha + b[i]
        if value > 0.0:
            out[i] = value
        else:
            out[i] = b[i] - alpha
"""


def test_find_metal_compiler_returns_path_or_none():
    metal = find_metal_compiler()

    assert metal is None or metal.endswith("metal")


def _require_usable_metal_toolchain(tmp_path):
    if find_metal_compiler() is None:
        pytest.fail("Metal compiler unavailable")
    try:
        compile_metal_source_to_air(
            MINIMAL_METAL_KERNEL,
            tmp_path / "metal_toolchain_probe.air",
        )
    except (MetalToolchainUnavailable, MetalCompileError) as exc:
        pytest.fail(f"Metal compiler unavailable: {exc}")


def test_compile_minimal_metal_kernel_when_toolchain_available(tmp_path):
    out = tmp_path / "pcc_empty.air"

    try:
        compile_metal_source_to_air(MINIMAL_METAL_KERNEL, out)
    except (MetalToolchainUnavailable, MetalCompileError) as exc:
        pytest.fail(f"Metal compiler unavailable: {exc}")

    assert out.is_file()
    assert out.stat().st_size > 0


def test_gpu_kernel_source_stripping_removes_device_only_surface():
    import ast

    module = ast.parse(PCC_GPU_VECTOR_ADD_LAUNCH)
    stripped = strip_gpu_kernel_host_source(PCC_GPU_VECTOR_ADD_LAUNCH, module)

    assert "@gpu.kernel" not in stripped
    assert "from pcc import gpu" not in stripped
    assert "def main" in stripped
    assert "from pcc.extern import extern, c_int64" in stripped
    assert "gpu.run_add_f32_demo()" not in stripped
    assert "__pcc_gpu_run_add_f32_demo()" in stripped


def test_gpu_kernel_vector_add_imports_to_kernel_ir_before_metal():
    import ast

    module = ast.parse(PCC_GPU_VECTOR_ADD)
    kernel = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    kernel_ir = lower_function_to_kernel_ir(kernel)
    data = kernel_ir.to_dict()

    assert data["module"] == "add_gpu_kernel_ir"
    assert data["funcs"][0]["name"] == "add"
    assert [param["kind"] for param in data["funcs"][0]["params"]] == [
        "buffer",
        "buffer",
        "buffer",
        "scalar",
    ]
    assert [op["op"] for op in data["funcs"][0]["body"]] == [
        "parallel",
        "elementwise_add",
    ]

    metal_source = lower_function_to_metal(kernel)
    assert "pcc route: Kernel IR -> TIRx -> Metal" in metal_source
    assert "kernel void add" in metal_source
    assert "out[i] = (a[i] + b[i]);" in metal_source


def test_gpu_kernel_structured_subset_has_no_direct_ast_to_metal_route():
    import ast

    module = ast.parse(PCC_GPU_STRUCTURED_SAXPY)
    kernel = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    kernel_ir = lower_function_to_kernel_ir(kernel)
    ops = kernel_ir.to_dict()["funcs"][0]["body"]

    assert [op["op"] for op in ops] == [
        "scalar_assign",
        "if_begin",
        "scalar_assign",
        "if_begin",
        "indexed_store",
        "else",
        "indexed_store",
        "if_end",
        "if_end",
    ]
    assert ops[0]["attrs"]["expr"] == {"kind": "thread_id_x"}
    assert ops[2]["attrs"]["dtype"] == "f32"

    metal_source = lower_function_to_metal(kernel)
    assert "pcc route: Kernel IR -> TIRx -> Metal" in metal_source
    assert "uint i = gid;" in metal_source
    assert "float value = ((a[i] * alpha) + b[i]);" in metal_source
    assert "out[i] = value;" in metal_source
    assert "out[i] = (b[i] - alpha);" in metal_source


def test_gpu_kernel_unsupported_syntax_fails_closed_without_metal_fallback():
    import ast

    module = ast.parse(
        "@gpu.kernel\n"
        "def bad(out: gpu.ptr_f32, n: gpu.u32):\n"
        "    for i in range(n):\n"
        "        out[i] = 0.0\n"
    )
    kernel = module.body[0]
    assert isinstance(kernel, ast.FunctionDef)
    with pytest.raises(GpuKernelError, match="no direct Metal fallback exists"):
        lower_function_to_metal(kernel)


def test_prepare_gpu_kernel_generates_metal_sidecar_artifacts(tmp_path):
    try:
        host_source, artifacts = prepare_gpu_kernels_for_source(
            PCC_GPU_VECTOR_ADD,
            "kernel_host.py",
            backend="metal",
            artifact_dir=tmp_path / "gpu",
            metallib_path=tmp_path / "kernel_host.metallib",
        )
    except (MetalToolchainUnavailable, MetalCompileError) as exc:
        pytest.fail(f"Metal compiler unavailable: {exc}")

    assert "def main" in host_source
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.name == "add"
    assert artifact.metal_path.is_file()
    assert artifact.air_path.is_file()
    assert artifact.metallib_path.is_file()
    metal_text = artifact.metal_path.read_text(encoding="utf-8")
    assert "kernel void add" in metal_text
    assert "thread_position_in_grid" in metal_text
    assert "out[i] = (a[i] + b[i]);" in metal_text


def test_pcc_emit_llvm_with_gpu_kernel_generates_metallib(tmp_path):
    _require_usable_metal_toolchain(tmp_path)
    src = tmp_path / "kernel_host.py"
    ll = tmp_path / "kernel_host.ll"
    src.write_text(PCC_GPU_VECTOR_ADD, encoding="utf-8")

    result = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--gpu-backend=metal",
            "--emit-llvm",
            str(ll),
            str(src),
        ],
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert ll.is_file()
    assert (tmp_path / "kernel_host.ll.metallib").is_file()
    assert (tmp_path / "kernel_host.ll.gpu" / "add.metal").is_file()


def test_pcc_metal_gpu_kernel_binary_launches_embedded_metallib(tmp_path):
    _require_usable_metal_toolchain(tmp_path)
    src = tmp_path / "kernel_host.py"
    exe = tmp_path / "kernel_host"
    src.write_text(PCC_GPU_VECTOR_ADD_LAUNCH, encoding="utf-8")

    compile_result = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--gpu-backend=metal",
            str(src),
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=180,
    )

    assert compile_result.returncode == 0, compile_result.stderr
    assert exe.is_file()
    sidecar = tmp_path / "kernel_host.metallib"
    assert sidecar.is_file()
    assert (tmp_path / "kernel_host.gpu" / "pcc_metal_runtime.o").is_file()
    sidecar.rename(tmp_path / "kernel_host.hidden.metallib")

    run_result = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
    )
    combined = run_result.stdout + run_result.stderr
    if "pcc metal: no Metal device available" in combined:
        pytest.fail("Metal runtime device unavailable")

    assert run_result.returncode == 0, combined
    assert "OK Metal add kernel via embedded metallib" in combined
    assert combined.strip().endswith("0")


def test_pcc_metal_gpu_kernel_launches_with_self_backend(tmp_path):
    if sys.platform != "darwin":
        pytest.fail("self backend Metal launch is currently Darwin-only")
    _require_usable_metal_toolchain(tmp_path)
    src = tmp_path / "kernel_host.py"
    exe = tmp_path / "kernel_host_self"
    src.write_text(PCC_GPU_VECTOR_ADD_LAUNCH, encoding="utf-8")

    compile_result = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--gpu-backend=metal",
            str(src),
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=240,
    )

    assert compile_result.returncode == 0, compile_result.stderr
    assert exe.is_file()
    sidecar = tmp_path / "kernel_host_self.metallib"
    assert sidecar.is_file()
    sidecar.rename(tmp_path / "kernel_host_self.hidden.metallib")

    run_result = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
    )
    combined = run_result.stdout + run_result.stderr
    if "pcc metal: no Metal device available" in combined:
        pytest.fail("Metal runtime device unavailable")

    assert run_result.returncode == 0, combined
    assert "OK Metal add kernel via embedded metallib" in combined
    assert combined.strip().endswith("0")
