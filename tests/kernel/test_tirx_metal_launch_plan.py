"""Metal launch planning for TIRx/Kernel IR.

This does not execute a GPU command buffer. It validates the runtime launch
packet and records the command-encoder plan a real Metal executor must follow.
"""

import pytest

from pcc.backend.metal import MetalCompileError, MetalToolchainUnavailable
from pcc.kernel_ir.hmm_fence import (
    HmmFenceError,
    PccBufferHandle,
    PccDeferredFreeQueue,
    PccFenceToken,
    PccPackedArgs,
)
from pcc.kernel_ir.ir import (
    BufferParam,
    KernelFunc,
    KernelModule,
    KernelOp,
    MemoryScope,
    ScalarParam,
    ScalarType,
)
from pcc.kernel_ir.metal_launch import (
    STATUS_BRIDGE_OBJECT_PRODUCED,
    STATUS_BRIDGE_SOURCE_ONLY,
    STATUS_PLAN_ONLY,
    STATUS_SKIPPED_WITH_REASON,
    MetalLaunchError,
    build_metal_executor_bridge_artifacts,
    emit_metal_executor_bridge_source,
    plan_metal_launch,
    prepare_metal_launch,
    write_metal_executor_bridge_source,
)
from pcc.kernel_ir.tilelang_import import import_tilelang_source


TILELANG_METAL_MATMUL = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_simdgroup(M, N, K, block_M=64, block_N=64, block_K=32, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype, scope="shared")
            B_shared = T.alloc_shared((block_K, block_N), dtype, scope="shared")
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=0):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


def _matmul_module() -> KernelModule:
    return import_tilelang_source(
        TILELANG_METAL_MATMUL,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 128, "N": 256, "K": 64},
        module_name="tilelang_metal_matmul",
    )


def _matmul_args(*, a_nbytes: int = 128 * 64 * 2) -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=a_nbytes, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=64 * 256 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=128 * 256 * 4, dtype="f32", device="metal:0"))
    return args


def _copy_module() -> KernelModule:
    func = KernelFunc(
        name="copy_kernel",
        params=(
            BufferParam("src", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
            BufferParam("dst", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
            ScalarParam("n", ScalarType.U32),
        ),
        body=(
            KernelOp("parallel", ("src", "dst"), {"extent": 16}),
            KernelOp("copy", ("src", "dst")),
        ),
        grid=(1,),
        threads=16,
    )
    return KernelModule("copy_mod", funcs=(func,))


def _main_copy_module() -> KernelModule:
    func = KernelFunc(
        name="main",
        params=(
            BufferParam("src", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
            BufferParam("dst", ScalarType.F32, rank=1, scope=MemoryScope.GLOBAL),
        ),
        body=(
            KernelOp("parallel", ("src", "dst"), {"extent": 8}),
            KernelOp("copy", ("src", "dst")),
        ),
        grid=(1,),
        threads=8,
    )
    return KernelModule("main_copy_mod", funcs=(func,))


def _main_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=8 * 4, dtype="f32", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=8 * 4, dtype="f32", device="metal:0"))
    return args


def _metadata_i16_module() -> KernelModule:
    func = KernelFunc(
        name="metadata_i16",
        params=(
            BufferParam("meta", ScalarType.I16, rank=2, shape=(2, 4), scope=MemoryScope.GLOBAL),
        ),
        body=(KernelOp("parallel", ("meta",), {"extent": 8}),),
        grid=(1,),
        threads=8,
    )
    return KernelModule("metadata_i16_mod", funcs=(func,))


def _metadata_i16_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=2 * 4 * 2, dtype="i16", device="metal:0"))
    return args


def test_imported_tilelang_matmul_launch_plan_validates_dispatch_and_args():
    plan = plan_metal_launch(
        _matmul_module(),
        _matmul_args(),
        metallib_path="/does/not/exist/tilelang_metal_matmul.metallib",
    )
    data = plan.to_dict()

    assert data["claim_mode"] == "Metal launch plan, not executed"
    assert data["kernel_entry"] == "gemm_kernel"
    assert data["launcher_symbol"] == "__pcc_launch_gemm_kernel_metal"
    assert data["runtime_launch_executed"] is False
    assert data["whole_program_gpu"] is False
    assert data["fence_required_on_commit"] is True
    assert data["metallib_available"] is False
    assert data["dispatch"] == {
        "api": "dispatchThreadgroups",
        "threadgroups_per_grid": [4, 2, 1],
        "threads_per_threadgroup": [128, 1, 1],
    }
    assert [arg["name"] for arg in data["args"]] == ["A", "B", "C"]
    assert [arg["required_nbytes"] for arg in data["args"]] == [16384, 32768, 131072]
    assert [arg["provided_nbytes"] for arg in data["args"]] == [16384, 32768, 131072]

    steps = data["command_encoder_steps"]
    assert [s["step"] for s in steps[:7]] == [
        "load_metallib",
        "newFunctionWithName",
        "newComputePipelineState",
        "newCommandQueue",
        "commandBuffer",
        "computeCommandEncoder",
        "setComputePipelineState",
    ]
    assert [s["step"] for s in steps if s["step"] == "setBuffer"] == [
        "setBuffer",
        "setBuffer",
        "setBuffer",
    ]
    assert any(s["step"] == "dispatchThreadgroups" for s in steps)
    assert steps[-1]["step"] == "complete_fence_on_command_buffer_completion"


def test_launch_plan_computes_narrow_integer_buffer_nbytes():
    plan = plan_metal_launch(
        _metadata_i16_module(),
        _metadata_i16_args(),
        metallib_path="/does/not/exist/metadata_i16.metallib",
    )
    data = plan.to_dict()

    assert [arg["dtype"] for arg in data["args"]] == ["i16"]
    assert [arg["required_nbytes"] for arg in data["args"]] == [16]
    assert [arg["provided_nbytes"] for arg in data["args"]] == [16]


def test_launch_plan_uses_legal_metal_entry_for_logical_main():
    plan = plan_metal_launch(
        _main_copy_module(),
        _main_copy_args(),
        entry="main",
        metallib_path="/does/not/exist/main_copy_mod.metallib",
    )
    source = emit_metal_executor_bridge_source(plan)

    assert plan.kernel_entry == "pcc_main_kernel"
    assert plan.launcher_symbol == "__pcc_launch_main_metal"
    assert 'newFunctionWithName:@"pcc_main_kernel"' in source
    assert 'newFunctionWithName:@"main"' not in source


def test_prepare_plan_only_does_not_execute_even_when_metallib_exists(tmp_path):
    metallib = tmp_path / "tilelang_metal_matmul.metallib"
    metallib.write_bytes(b"fake metallib for launch-plan test")

    result = prepare_metal_launch(
        _matmul_module(),
        _matmul_args(),
        metallib_path=metallib,
        execute=False,
    )
    data = result.to_dict()

    assert data["status"] == STATUS_PLAN_ONLY
    assert data["runtime_launch_executed"] is False
    assert data["plan"]["metallib_available"] is True
    assert "execute=False" in data["reason"]


def test_execute_request_skips_without_metallib():
    result = prepare_metal_launch(
        _matmul_module(),
        _matmul_args(),
        metallib_path="/does/not/exist/tilelang_metal_matmul.metallib",
        execute=True,
    )

    assert result.status == STATUS_SKIPPED_WITH_REASON
    assert result.runtime_launch_executed is False
    assert "no usable metallib" in result.reason


def test_execute_request_skips_until_kernel_ir_executor_exists(tmp_path):
    metallib = tmp_path / "tilelang_metal_matmul.metallib"
    metallib.write_bytes(b"fake metallib for launch-plan test")

    result = prepare_metal_launch(
        _matmul_module(),
        _matmul_args(),
        metallib_path=metallib,
        execute=True,
    )

    assert result.status == STATUS_SKIPPED_WITH_REASON
    assert result.runtime_launch_executed is False
    assert "executor is not implemented" in result.reason


def test_executor_bridge_source_maps_plan_to_objc_command_buffer_calls():
    plan = plan_metal_launch(
        _matmul_module(),
        _matmul_args(),
        metallib_path="/does/not/exist/tilelang_metal_matmul.metallib",
    )
    source = emit_metal_executor_bridge_source(plan)

    assert "#import <Metal/Metal.h>" in source
    assert "int64_t __pcc_launch_gemm_kernel_metal_runtime_bridge(" in source
    assert "id<MTLDevice> device = MTLCreateSystemDefaultDevice();" in source
    assert "id<MTLLibrary> library = [device newLibraryWithURL:url error:&error];" in source
    assert 'id<MTLFunction> function = [library newFunctionWithName:@"gemm_kernel"];' in source
    assert "newComputePipelineStateWithFunction:function" in source
    assert "id<MTLCommandQueue> queue = [device newCommandQueue];" in source
    assert "id<MTLCommandBuffer> command_buffer = [queue commandBuffer];" in source
    assert "id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];" in source
    assert "id<MTLBuffer> pcc_buf_A = (__bridge id<MTLBuffer>)buffer_handles[0];" in source
    assert "[encoder setBuffer:pcc_buf_A offset:0 atIndex:0];" in source
    assert "id<MTLBuffer> pcc_buf_B = (__bridge id<MTLBuffer>)buffer_handles[1];" in source
    assert "[encoder setBuffer:pcc_buf_B offset:0 atIndex:1];" in source
    assert "id<MTLBuffer> pcc_buf_C = (__bridge id<MTLBuffer>)buffer_handles[2];" in source
    assert "[encoder setBuffer:pcc_buf_C offset:0 atIndex:2];" in source
    assert "MTLSize grid = MTLSizeMake(4u, 2u, 1u);" in source
    assert "MTLSize threads = MTLSizeMake(128u, 1u, 1u);" in source
    assert "[encoder dispatchThreadgroups:grid threadsPerThreadgroup:threads];" in source
    assert "[command_buffer addCompletedHandler:^(id<MTLCommandBuffer> cb)" in source
    assert "fence_complete(fence_ctx);" in source
    assert "[command_buffer commit];" in source
    assert "runtime_launch_executed" not in source


def test_executor_bridge_source_handles_scalar_setbytes_for_copy_kernel():
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="metal:0"))
    args.add_scalar("u32", 16)
    plan = plan_metal_launch(_copy_module(), args)
    source = emit_metal_executor_bridge_source(plan)

    assert "const uint32_t *pcc_scalar_n = (const uint32_t *)scalar_values[0];" in source
    assert "[encoder setBytes:pcc_scalar_n length:sizeof(uint32_t) atIndex:2];" in source
    assert "MTLSize grid = MTLSizeMake(1u, 1u, 1u);" in source
    assert "MTLSize threads = MTLSizeMake(16u, 1u, 1u);" in source


def test_write_executor_bridge_source_artifact_is_source_only(tmp_path):
    plan = plan_metal_launch(_matmul_module(), _matmul_args())
    result = write_metal_executor_bridge_source(plan, tmp_path)
    data = result.to_dict()

    assert result.status == STATUS_BRIDGE_SOURCE_ONLY
    assert data["source_produced"] is True
    assert data["runtime_launch_executed"] is False
    path = tmp_path / "gemm_kernel_metal_bridge.m"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "int64_t __pcc_launch_gemm_kernel_metal_runtime_bridge(" in text
    assert "[command_buffer commit];" in text


def test_build_executor_bridge_artifacts_source_only_does_not_compile_or_launch(tmp_path):
    plan = plan_metal_launch(_matmul_module(), _matmul_args())
    result = build_metal_executor_bridge_artifacts(plan, tmp_path)
    data = result.to_dict()

    assert result.status == STATUS_BRIDGE_SOURCE_ONLY
    assert data["source_produced"] is True
    assert data["bridge_object_produced"] is False
    assert data["runtime_launch_executed"] is False
    assert "compile_bridge=False" in data["reason"]
    assert result.object_path is None
    assert (tmp_path / "gemm_kernel_metal_bridge.m").is_file()


def test_build_executor_bridge_artifacts_compiles_object_with_injected_compiler(tmp_path):
    plan = plan_metal_launch(_matmul_module(), _matmul_args())
    calls = {}

    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        calls["output_path"] = output_path
        calls["source_path"] = source_path
        calls["timeout"] = timeout
        assert source_path is not None
        assert source_path.is_file()
        output_path.write_bytes(b"fake objc object")
        return output_path

    result = build_metal_executor_bridge_artifacts(
        plan,
        tmp_path,
        compile_bridge=True,
        compiler=fake_compiler,
        timeout=7.0,
    )
    data = result.to_dict()

    assert result.status == STATUS_BRIDGE_OBJECT_PRODUCED
    assert data["source_produced"] is True
    assert data["bridge_object_produced"] is True
    assert data["runtime_launch_executed"] is False
    assert data["object_path"].endswith("gemm_kernel_metal_bridge.o")
    assert calls["timeout"] == 7.0
    assert calls["source_path"] == tmp_path / "gemm_kernel_metal_bridge.m"
    assert calls["output_path"] == tmp_path / "gemm_kernel_metal_bridge.o"
    assert (tmp_path / "gemm_kernel_metal_bridge.o").read_bytes() == b"fake objc object"


def test_build_executor_bridge_artifacts_skips_when_bridge_compiler_unavailable(tmp_path):
    plan = plan_metal_launch(_matmul_module(), _matmul_args())

    def missing_compiler(output_path, *, source_path=None, timeout=30.0):
        raise MetalToolchainUnavailable("xcrun clang missing")

    result = build_metal_executor_bridge_artifacts(
        plan,
        tmp_path,
        compile_bridge=True,
        compiler=missing_compiler,
    )
    data = result.to_dict()

    assert result.status == STATUS_SKIPPED_WITH_REASON
    assert data["source_produced"] is True
    assert data["bridge_object_produced"] is False
    assert data["runtime_launch_executed"] is False
    assert "compiler unavailable" in data["reason"]
    assert "xcrun clang missing" in data["reason"]
    assert (tmp_path / "gemm_kernel_metal_bridge.m").is_file()


def test_build_executor_bridge_artifacts_rejects_compiler_source_errors(tmp_path):
    plan = plan_metal_launch(_matmul_module(), _matmul_args())

    def rejecting_compiler(output_path, *, source_path=None, timeout=30.0):
        raise MetalCompileError("invalid Objective-C")

    with pytest.raises(MetalLaunchError, match="failed to compile"):
        build_metal_executor_bridge_artifacts(
            plan,
            tmp_path,
            compile_bridge=True,
            compiler=rejecting_compiler,
        )
    assert (tmp_path / "gemm_kernel_metal_bridge.m").is_file()


def test_launch_plan_rejects_too_small_static_shape_buffer():
    with pytest.raises(MetalLaunchError, match="requires at least 16384 bytes"):
        plan_metal_launch(_matmul_module(), _matmul_args(a_nbytes=16383))


def test_launch_plan_rejects_pending_free_buffer():
    args = _matmul_args()
    fence = PccFenceToken()
    q = PccDeferredFreeQueue()
    q.schedule_free(args.buffers[0], fence)

    with pytest.raises(MetalLaunchError, match="pending_free"):
        plan_metal_launch(_matmul_module(), args)


def test_launch_plan_rejects_non_metal_launch_device():
    args = PccPackedArgs(launch_device="cpu")
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="cpu"))
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="cpu"))
    args.add_scalar("u32", 16)

    with pytest.raises(MetalLaunchError, match="launch_device='metal"):
        plan_metal_launch(_copy_module(), args)


def test_launch_plan_rejects_pyobject_scalar_before_encoding():
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="metal:0"))
    args.add_scalar("u32", [16])

    with pytest.raises(HmmFenceError, match="PyObject"):
        plan_metal_launch(_copy_module(), args)
