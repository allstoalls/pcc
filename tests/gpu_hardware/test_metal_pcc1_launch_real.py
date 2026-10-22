"""Level-5 pcc1-native Metal launcher gate.

This file is intentionally separate from Level-4 hardware gates. A host Python
test harness that submits a real Metal command buffer proves
GPU_LEVEL_4_DEVICE_RESULT, not GPU_LEVEL_5_PCC1_NATIVE.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import struct
import sys
import textwrap
from pathlib import Path

import pytest

from pcc.kernel_ir.cpu_reference import execute_scalar_tiled_gemm_reference
from pcc.kernel_ir.hmm_fence import PccBufferHandle, PccPackedArgs
from pcc.kernel_ir.gpu_claims import (
    GpuClaimError,
    GpuClaimLevel,
    STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
    classify_pcc1_native_gpu_result,
    require_pcc1_native_or_skip,
)
from pcc.kernel_ir.gpu_owner_backend import (
    GPU_BACKEND_PCC_METAL,
    GPU_BACKEND_TVM_TILELANG,
    pcc_metal_owner_identity_fields,
    tvm_tilelang_owner_identity_fields,
    validate_gpu_owner_identity,
)
from pcc.kernel_ir.ir import (
    BufferParam,
    KernelFunc,
    KernelModule,
    KernelOp,
    Layout,
    LocalBuffer,
    MemoryScope,
    ScalarParam,
    ScalarType,
)
from pcc.kernel_ir.metal_buffer import (
    STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED,
    build_metal_native_buffer_runtime_artifacts,
)
from pcc.kernel_ir.metal_finalize import (
    emit_metal_simdgroup_gemm_source,
    emit_metal_source,
)
from pcc.kernel_ir.metal_package import build_metal_kernel_package
from pcc.kernel_ir.metal_invoke import STATUS_BRIDGE_INVOKED
from pcc.kernel_ir.tirx_adapter import lower_to_plain_tir
from pcc.kernel_ir.tvm_tilelang_owner import (
    TVM_TILELANG_PIPELINE,
    compile_with_tvm_tilelang_provider,
)
from pcc.kernel_ir.pcc1_metal_preflight import (
    PCC1_METAL_RUNTIME_ABI_ENTRY_MODULES,
    STATUS_PCC1_METAL_PREFLIGHT_BLOCKED,
    STATUS_PCC1_METAL_PREFLIGHT_READY,
    analyze_pcc1_metal_launcher_preflight,
)
from pcc.kernel_ir.metal_source_runtime import (
    STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED,
    STATUS_SKIPPED_WITH_REASON,
    STATUS_SOURCE_RUNTIME_INVOKED,
    STATUS_SOURCE_RUNTIME_PACKAGE_EXECUTED,
    build_metal_source_runtime_bridge_artifacts,
)
from pcc.kernel_ir.tilelang_import import import_tilelang_source


REPO = Path(__file__).resolve().parents[2]
_FRESHNESS_SUFFIXES = {".py", ".c", ".h"}
_IGNORED_FRESHNESS_DIRS = {"__pycache__", "build", "build_py"}

_TILELANG_PCC1_MATMUL_SOURCE = """
import tilelang
import tilelang.language as T

pytestmark = [
    pytest.mark.pcc_gate(probe="metal"),
    pytest.mark.pcc_gate(probe="pcc1"),
]


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


_TILELANG_PCC1_OUTPUT_STAGED_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "def matmul_simdgroup",
        "def matmul_output_staging",
        1,
    )
    .replace(
        "            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)\n"
        "            T.clear(C_local)",
        "            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)\n"
        "            C_shared = T.alloc_shared((block_M, block_N), accum_dtype)\n"
        "            T.clear(C_local)",
    )
    .replace(
        "            T.copy(C_local, C[by * block_M, bx * block_N])",
        "            T.copy(C_local, C_shared)\n"
        "            T.copy(C_shared, C[by * block_M, bx * block_N])",
    )
)


_TILELANG_PCC1_OUTPUT_STAGED_F16_TRANSPOSE_B_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_output_staging_f16_transpose_b(
    M,
    N,
    K,
    block_M=8,
    block_N=8,
    block_K=8,
    num_stages=0,
    thread_num=32,
    block_rows=2,
    block_cols=1,
    enable_rasteration=True,
    dtype=T.float16,
    accum_dtype=T.float32,
):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((N, K), dtype),
        C: T.Tensor((M, N), dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=thread_num) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_N, block_K), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            C_shared = T.alloc_shared((block_M, block_N), dtype)
            T.use_swizzle(panel_size=10, enable=enable_rasteration)
            T.clear(C_local)
            for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                T.copy(B[bx * block_N, ko * block_K], B_shared)
                T.gemm(
                    A_shared,
                    B_shared,
                    C_local,
                    transpose_B=True,
                    policy=T.GemmWarpPolicy.from_warp_partition(block_rows, block_cols),
                )
            T.copy(C_local, C_shared)
            T.copy(C_shared, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_OUTPUT_STAGED_F16_TRANSPOSE_B_POLICY_ALIAS_SOURCE = (
    _TILELANG_PCC1_OUTPUT_STAGED_F16_TRANSPOSE_B_SOURCE.replace(
        "import tilelang.language as T",
        "import tilelang.language as T\nfrom tilelang.tileop.base import GemmWarpPolicy",
    ).replace(
        "T.GemmWarpPolicy.from_warp_partition(block_rows, block_cols)",
        "GemmWarpPolicy.FullRow",
    )
)


_TILELANG_PCC1_VECTORIZED_ABC_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_vectorized_abc_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(1, T.ceildiv(K, block_K)):
                for i in T.Parallel(block_M):
                    for kk in T.vectorized(block_K):
                        T.copy(A[by * block_M + i, ko * block_K + kk], A_shared)
                for kk in T.Parallel(block_K):
                    for j in T.vectorized(block_N):
                        T.copy(B[ko * block_K + kk, bx * block_N + j], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            for i in T.Parallel(block_M):
                for j in T.vectorized(block_N):
                    T.copy(C_local, C[by * block_M + i, bx * block_N + j])
    return gemm_kernel
"""


_TILELANG_PCC1_VECTORIZED_A_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_vectorized_a_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                for i in T.Parallel(block_M):
                    for kk in T.vectorized(block_K):
                        T.copy(A[by * block_M + i, ko * block_K + kk], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_VECTORIZED_B_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_vectorized_b_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                for kk in T.Parallel(block_K):
                    for j in T.vectorized(block_N):
                        T.copy(B[ko * block_K + kk, bx * block_N + j], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_VECTORIZED_C_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_vectorized_c_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            for i in T.Parallel(block_M):
                for j in T.vectorized(block_N):
                    T.copy(C_local, C[by * block_M + i, bx * block_N + j])
    return gemm_kernel
"""


_TILELANG_PCC1_PARALLEL_A_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_parallel_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                for i, kk in T.Parallel(block_M, block_K):
                    T.copy(A[by * block_M + i, ko * block_K + kk], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_PARALLEL_B_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_parallel_b_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                for kk, j in T.Parallel(block_K, block_N):
                    T.copy(B[ko * block_K + kk, bx * block_N + j], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_PARALLEL_C_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_parallel_c_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            for i, j in T.Parallel(block_M, block_N):
                T.copy(C_local, C[by * block_M + i, bx * block_N + j])
    return gemm_kernel
"""


_TILELANG_PCC1_PARALLEL_AB_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_parallel_ab_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                for i, kk in T.Parallel(block_M, block_K):
                    T.copy(A[by * block_M + i, ko * block_K + kk], A_shared)
                for kk, j in T.Parallel(block_K, block_N):
                    T.copy(B[ko * block_K + kk, bx * block_N + j], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_PARALLEL_ABC_COPY_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_parallel_abc_copy(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                for i, kk in T.Parallel(block_M, block_K):
                    T.copy(A[by * block_M + i, ko * block_K + kk], A_shared)
                for kk, j in T.Parallel(block_K, block_N):
                    T.copy(B[ko * block_K + kk, bx * block_N + j], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            for i, j in T.Parallel(block_M, block_N):
                T.copy(C_local, C[by * block_M + i, bx * block_N + j])
    return gemm_kernel
"""


_TILELANG_PCC1_SPLITK_ATOMIC_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_splitk_atomic(M, N, K, split_k=2, block_M=8, block_N=8, block_K=4, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), split_k, threads=32) as (bx, by, bz):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.Pipelined(T.ceildiv(K // split_k, block_K), num_stages=0):
                T.copy(A[by * block_M, bz * (K // split_k) + ko * block_K], A_shared)
                T.copy(B[bz * (K // split_k) + ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            for i, j in T.Parallel(block_M, block_N):
                T.atomic_add(C[by * block_M + i, bx * block_N + j], C_local[i, j])
    return gemm_kernel
"""


_TILELANG_PCC1_SPLITK_ATOMIC_CEILDIV_SOURCE = (
    _TILELANG_PCC1_SPLITK_ATOMIC_SOURCE.replace(
        "K // split_k",
        "T.ceildiv(K, split_k)",
    )
)


_TILELANG_PCC1_SPLITK_ATOMIC_FLOOR_PLUS_ONE_CEILDIV_SOURCE = (
    _TILELANG_PCC1_SPLITK_ATOMIC_SOURCE.replace(
        "    @T.prim_func",
        "    splitK = (K - 1) // split_k + 1\n\n    @T.prim_func",
    ).replace(
        "K // split_k",
        "splitK",
    )
)


_TILELANG_PCC1_SPLITK_ATOMIC_VECTORIZED_C_SOURCE = (
    _TILELANG_PCC1_SPLITK_ATOMIC_SOURCE.replace(
        """            for i, j in T.Parallel(block_M, block_N):
                T.atomic_add(C[by * block_M + i, bx * block_N + j], C_local[i, j])""",
        """            for i in T.Parallel(block_M):
                for j in T.vectorized(block_N):
                    T.atomic_add(C[by * block_M + i, bx * block_N + j], C_local[i, j])""",
    ).replace("matmul_splitk_atomic", "matmul_splitk_atomic_vectorized_c", 1)
)


_TILELANG_PCC1_TRANSPOSE_AB_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_transpose_ab(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((K, M), dtype),
        B: T.Tensor((N, K), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_K, block_M), dtype)
            B_shared = T.alloc_shared((block_N, block_K), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                T.copy(A[ko * block_K, by * block_M], A_shared)
                T.copy(B[bx * block_N, ko * block_K], B_shared)
                T.gemm(A_shared, B_shared, C_local, True, True)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_TRANSPOSE_A_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_transpose_a(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((K, M), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_K, block_M), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                T.copy(A[ko * block_K, by * block_M], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local, True, False)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_TRANSPOSE_B_SOURCE = """
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_transpose_b(M, N, K, block_M=8, block_N=8, block_K=8, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def gemm_kernel(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((N, K), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=32) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_N, block_K), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.serial(T.ceildiv(K, block_K)):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                T.copy(B[bx * block_N, ko * block_K], B_shared)
                T.gemm(A_shared, B_shared, C_local, False, True)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return gemm_kernel
"""


_TILELANG_PCC1_SWIZZLED_PADDED_ANNOTATE_LAYOUT_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "T.clear(C_local)",
        (
            "T.annotate_layout({\n"
            "                A_shared: tilelang.layout.make_swizzled_layout(A_shared),\n"
            "                B_shared: tilelang.layout.make_swizzled_layout(B_shared),\n"
            "            })\n"
            "            T.clear(C_local)"
        ),
    )
)


_TILELANG_PCC1_ENABLED_SWIZZLE_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "T.clear(C_local)",
        "T.use_swizzle(panel_size=2, enable=True)\n            T.clear(C_local)",
    )
)


_TILELANG_PCC1_NONZERO_START_PIPELINED_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "T.Pipelined(T.ceildiv(K, block_K), num_stages=0)",
        "T.Pipelined(1, T.ceildiv(K, block_K), num_stages=0)",
    )
)


_TILELANG_PCC1_STEP_SERIAL_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=0):",
        "for ko in T.serial(0, T.ceildiv(K, block_K), 2):",
    )
)


_TILELANG_PCC1_STEP_PIPELINED_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "T.Pipelined(T.ceildiv(K, block_K), num_stages=0)",
        "T.Pipelined(0, T.ceildiv(K, block_K), 2, num_stages=0)",
    )
)


_TILELANG_PCC1_NONZERO_STEP_PIPELINED_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "T.Pipelined(T.ceildiv(K, block_K), num_stages=0)",
        "T.Pipelined(1, T.ceildiv(K, block_K), 2, num_stages=0)",
    )
)


_TILELANG_PCC1_NONZERO_STEP_SERIAL_SOURCE = (
    _TILELANG_PCC1_MATMUL_SOURCE.replace(
        "for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=0):",
        "for ko in T.serial(1, T.ceildiv(K, block_K), 2):",
    )
)


def _freshness_cutoff(repo: Path) -> float:
    mtimes: list[float] = []
    pcc_dir = repo / "pcc"
    for suffix in _FRESHNESS_SUFFIXES:
        for path in pcc_dir.rglob("*" + suffix):
            if not path.is_file():
                continue
            if any(part in _IGNORED_FRESHNESS_DIRS for part in path.parts):
                continue
            mtimes.append(path.stat().st_mtime)
    return max(mtimes) if mtimes else 0.0


def _current_pcc1_candidates(repo: Path) -> tuple[Path, ...]:
    fixed = (
        repo / "pcc1",
        repo / "build" / "bootstrap-pytest-self" / "pcc1",
        repo / "build" / "bootstrap" / "pcc1",
        repo / "build" / "bootstrap-self-darwin_arm64" / "pcc1",
        repo / "build" / "bootstrap-llvm-darwin_arm64" / "pcc1",
    )
    dynamic = tuple(
        sorted(
            (repo / "build").glob("bootstrap-*/pcc1"),
            key=lambda path: (path.stat().st_mtime, str(path)),
            reverse=True,
        )
    )
    seen: set[Path] = set()
    out: list[Path] = []
    for candidate in fixed + dynamic:
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return tuple(out)


def _find_current_pcc1(repo: Path) -> Path | None:
    explicit = os.environ.get("PCC_CURRENT_PCC1")
    if explicit:
        raw = Path(explicit)
        candidates = (raw,) if raw.is_absolute() else (raw, repo / raw)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None
    cutoff = _freshness_cutoff(repo)
    for candidate in _current_pcc1_candidates(repo):
        if candidate.exists() and candidate.stat().st_mtime >= cutoff:
            return candidate
    return None


def _cc() -> str:
    cc = shutil.which("cc")
    if cc is None:
        pytest.fail("cc is required for the pcc1 Metal runtime C-shim gate")
    return cc


def _links_libpython(path: Path) -> bool:
    cmd = ["otool", "-L", str(path)] if sys.platform == "darwin" else [
        "ldd",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.fail(f"can't run {cmd[0]}; cannot verify libpython linkage")
    assert proc.returncode == 0, (
        f"{cmd[0]} failed for {path}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    return "libpython" in text.lower() or "Python.framework" in text


def _build_fake_runtime_source_bridge(tmp_path: Path) -> Path:
    if sys.platform == "darwin":
        bridge = tmp_path / "fake_metal_bridge.dylib"
        bridge_cmd = [_cc(), "-dynamiclib", "-o", str(bridge)]
    elif sys.platform.startswith("linux"):
        bridge = tmp_path / "fake_metal_bridge.so"
        bridge_cmd = [_cc(), "-shared", "-fPIC", "-o", str(bridge)]
    else:
        pytest.fail("dlfcn-based pcc1 Metal runtime C-shim gate is Darwin/Linux only")

    bridge_source = tmp_path / "fake_metal_bridge.c"
    bridge_source.write_text(
        textwrap.dedent(
            r"""
            #include <stdbool.h>
            #include <stdint.h>
            #include <stdlib.h>
            #include <string.h>

            typedef struct PccFakeBuffer {
                uint64_t nbytes;
                uint8_t storage[128];
            } PccFakeBuffer;

            int64_t pcc_fake_source_bridge(
                const char *source,
                uint64_t len,
                void **buffers,
                void **scalars,
                void (*fence_complete)(void *),
                void *fence_context,
                bool wait_until_completed
            ) {
                uint32_t u32_value = 0;
                double f64_value = 0.0;
                bool bool_value = false;

                if (source == 0 || len == 0 || source[0] != 'k') return 10;
                if (buffers == 0 || buffers[0] == 0) return 11;
                if (scalars == 0 || scalars[0] == 0 || scalars[1] == 0 || scalars[2] == 0) return 12;
                if (fence_complete != 0 || fence_context != 0) return 13;
                if (!wait_until_completed) return 14;

                memcpy(&u32_value, scalars[0], sizeof(u32_value));
                memcpy(&f64_value, scalars[1], sizeof(f64_value));
                memcpy(&bool_value, scalars[2], sizeof(bool_value));
                if (u32_value != 7u) return 15;
                if (f64_value != 1.5) return 16;
                if (!bool_value) return 17;
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_create(uint64_t nbytes, void **out_buffer) {
                if (out_buffer == 0) return 20;
                if (nbytes == 0 || nbytes > 128) return 21;
                PccFakeBuffer *buffer = (PccFakeBuffer *)calloc(1, sizeof(PccFakeBuffer));
                if (buffer == 0) return 28;
                buffer->nbytes = 128;
                *out_buffer = buffer;
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_length(void *buffer, uint64_t *out_nbytes) {
                if (buffer == 0 || out_nbytes == 0) return 22;
                PccFakeBuffer *fake = (PccFakeBuffer *)buffer;
                *out_nbytes = fake->nbytes;
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_write(
                void *buffer, uint64_t offset, const void *src, uint64_t nbytes) {
                if (buffer == 0 || src == 0) return 23;
                PccFakeBuffer *fake = (PccFakeBuffer *)buffer;
                if (offset > fake->nbytes || nbytes > fake->nbytes - offset) return 24;
                memcpy(fake->storage + offset, src, (size_t)nbytes);
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_read(
                void *buffer, uint64_t offset, void *dst, uint64_t nbytes) {
                if (buffer == 0 || dst == 0) return 25;
                PccFakeBuffer *fake = (PccFakeBuffer *)buffer;
                if (offset > fake->nbytes || nbytes > fake->nbytes - offset) return 26;
                memcpy(dst, fake->storage + offset, (size_t)nbytes);
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_release(void *buffer) {
                if (buffer == 0) return 27;
                free(buffer);
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [*bridge_cmd, str(bridge_source)],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return bridge


def _pcc1_launcher_gate_or_reason(repo: Path) -> Path | str:
    reasons: list[str] = []
    run_enabled = os.environ.get("PCC_RUN_GPU_PCC1_LAUNCH") == "1"
    strict = os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1"
    if not run_enabled:
        reasons.append("PCC_RUN_GPU_PCC1_LAUNCH=1 is not set")
    pcc1 = _find_current_pcc1(repo)
    if pcc1 is None:
        if strict or run_enabled:
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 launcher gate; "
                "set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        reasons.append("no fresh current pcc1 binary for GPU Level-5 launcher gate")
    elif run_enabled or strict:
        preflight = analyze_pcc1_metal_launcher_preflight(repo)
        if preflight.blocked:
            message = preflight.reason
            if strict:
                pytest.fail(message)
            reasons.append(message)
    if reasons:
        return "SKIPPED_WITH_REASON: " + "; ".join(reasons)
    assert pcc1 is not None
    return pcc1


def _level4_package_dict() -> dict[str, object]:
    return {
        "status": STATUS_SOURCE_RUNTIME_PACKAGE_EXECUTED,
        "runtime_launch_executed": True,
        "runtime_source_compiled": True,
        "whole_program_gpu": False,
        "finalize": {"metallib_produced": False},
        "invocation": {
            "status": STATUS_SOURCE_RUNTIME_INVOKED,
            "fence_completed": True,
        },
        "cpu_comparison": {"status": "metal_cpu_oracle_match"},
    }


def _copy_module() -> KernelModule:
    func = KernelFunc(
        name="copy_kernel",
        params=(
            BufferParam(
                "src",
                ScalarType.F32,
                rank=1,
                shape=(4,),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "dst",
                ScalarType.F32,
                rank=1,
                shape=(4,),
                scope=MemoryScope.GLOBAL,
            ),
            ScalarParam("n", ScalarType.U32),
        ),
        body=(
            KernelOp("parallel", ("src", "dst", "n"), {"extent": 4}),
            KernelOp("copy", ("src", "dst")),
        ),
        grid=(1,),
        threads=4,
    )
    return KernelModule("pcc1_copy_mod", funcs=(func,))


def _copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=16, dtype="f32", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16, dtype="f32", device="metal:0"))
    args.add_scalar("u32", 4)
    return args


def _build_real_runtime_source_copy_artifacts(tmp_path: Path):
    package = build_metal_kernel_package(
        _copy_module(),
        _copy_args(),
        tmp_path / "real_pcc1_package",
        compile_bridge=False,
    )
    metal_source = package.finalize.metal_source
    if not metal_source:
        pytest.fail("Metal finalize did not produce runtime-source copy kernel source")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    return package, native_runtime, source_bridge, metal_source


def _build_real_metallib_copy_artifacts(tmp_path: Path):
    package = build_metal_kernel_package(
        _copy_module(),
        _copy_args(),
        tmp_path / "real_pcc1_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(f"metallib package did not produce metallib: {package.to_dict()}")
    if not package.bridge_library_load_validated:
        pytest.fail(f"metallib bridge was not load-validated: {package.to_dict()}")
    if package.bridge_library_path is None:
        pytest.fail("metallib bridge did not produce a library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("metallib launch plan did not carry a metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    return package, native_runtime


def _build_real_metallib_tilelang_gemm_artifacts(tmp_path: Path):
    package = build_metal_kernel_package(
        _tilelang_gemm_module(),
        _tilelang_gemm_args(),
        tmp_path / "real_pcc1_tilelang_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            f"TileLang GEMM metallib package did not produce metallib: {package.to_dict()}"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            f"TileLang GEMM metallib bridge was not load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("TileLang GEMM metallib bridge did not produce a library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("TileLang GEMM launch plan did not carry a metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    return package, native_runtime


def _build_real_metallib_tilelang_local_metal_benchmark_artifacts(tmp_path: Path):
    module = _tilelang_local_metal_benchmark_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_local_metal_benchmark_args(),
        tmp_path / "real_pcc1_tilelang_local_metal_benchmark_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "Local TileLang Metal benchmark GEMM metallib package did not produce "
            f"metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail("Local TileLang Metal benchmark metallib package did not retain source")
    if "device float* C [[buffer(2)]]" not in package.finalize.metal_source:
        pytest.fail("Local TileLang Metal benchmark source lost f32 C buffer")
    if "B[(b_row * 7u) + b_col]" not in package.finalize.metal_source:
        pytest.fail("Local TileLang Metal benchmark source lost B(K,N) load")
    if "C[(row * 7u) + col] = (float)acc;" not in package.finalize.metal_source:
        pytest.fail("Local TileLang Metal benchmark source lost f32 C store")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "Local TileLang Metal benchmark GEMM metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("Local TileLang Metal benchmark metallib bridge missing library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("Local TileLang Metal benchmark launch plan missing metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_local_metal_benchmark_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _tilelang_output_staging_inputs()
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    return package, native_runtime, a, b, expected


def _build_real_metallib_tilelang_local_matmul_nonroller_artifacts(tmp_path: Path):
    module = _tilelang_local_matmul_nonroller_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_output_staging_f16_transpose_b_args(),
        tmp_path / "real_pcc1_tilelang_local_matmul_nonroller_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "Local TileLang matmul non-roller metallib package did not produce "
            f"metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail("Local TileLang matmul non-roller metallib package did not retain source")
    if "device half* C [[buffer(2)]]" not in package.finalize.metal_source:
        pytest.fail("Local TileLang matmul non-roller source lost f16 C buffer")
    if "swizzle_panel_size" in package.finalize.metal_source:
        pytest.fail("Local TileLang matmul non-roller source emitted disabled swizzle math")
    if "C[(row * 7u) + col] = (half)acc;" not in package.finalize.metal_source:
        pytest.fail("Local TileLang matmul non-roller source lost half C store")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "Local TileLang matmul non-roller metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("Local TileLang matmul non-roller metallib bridge missing library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("Local TileLang matmul non-roller launch plan missing metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_local_matmul_nonroller_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _tilelang_output_staging_f16_transpose_b_inputs()
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    return package, native_runtime, a, b, expected


def _build_real_metallib_tilelang_local_matmul_static_roller_artifacts(tmp_path: Path):
    module = _tilelang_local_matmul_static_roller_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_output_staging_f16_transpose_b_args(),
        tmp_path / "real_pcc1_tilelang_local_matmul_static_roller_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "Local TileLang matmul static roller-config metallib package did not "
            f"produce metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail("Local TileLang matmul static roller-config package did not retain source")
    if "device half* C [[buffer(2)]]" not in package.finalize.metal_source:
        pytest.fail("Local TileLang matmul static roller-config source lost f16 C buffer")
    if "swizzle_panel_size = 10u * swizzle_grid_x;" not in package.finalize.metal_source:
        pytest.fail("Local TileLang matmul static roller-config source lost enabled swizzle math")
    if "C[(row * 7u) + col] = (half)acc;" not in package.finalize.metal_source:
        pytest.fail("Local TileLang matmul static roller-config source lost half C store")
    if package.launch_plan.kernel_entry != "pcc_main_kernel":
        pytest.fail(
            "Local TileLang matmul static roller-config did not legalize logical "
            f"`main` to Metal entry pcc_main_kernel: {package.launch_plan.to_dict()}"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            "Local TileLang matmul static roller-config metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("Local TileLang matmul static roller-config bridge missing library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("Local TileLang matmul static roller-config launch plan missing metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_local_matmul_static_roller_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _tilelang_output_staging_f16_transpose_b_inputs()
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    return package, native_runtime, a, b, expected


def _tilelang_gemm_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_MATMUL_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 2, "N": 2, "K": 2},
        module_name="pcc1_tilelang_metal_matmul_runtime",
    )


def _tilelang_local_metal_benchmark_module() -> KernelModule:
    benchmark_path = (
        Path.home() / "tilelang" / "benchmark" / "matmul_metal" / "benchmark_matmul_metal.py"
    )
    if not benchmark_path.exists():
        pytest.fail(f"local TileLang Metal benchmark reference not found: {benchmark_path}")
    return import_tilelang_source(
        benchmark_path.read_text(encoding="utf-8"),
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={
            "M": 5,
            "N": 7,
            "K": 3,
            "block_M": 8,
            "block_N": 8,
            "block_K": 8,
        },
        module_name="pcc1_tilelang_local_metal_benchmark_runtime",
    )


def _tilelang_local_matmul_nonroller_module() -> KernelModule:
    benchmark_path = Path.home() / "tilelang" / "benchmark" / "matmul" / "benchmark_matmul.py"
    if not benchmark_path.exists():
        pytest.fail(f"local TileLang benchmark reference not found: {benchmark_path}")
    return import_tilelang_source(
        benchmark_path.read_text(encoding="utf-8"),
        outer_function="matmul",
        prim_func="main",
        constants={
            "M": 5,
            "N": 7,
            "K": 3,
            "with_roller": False,
            "block_M": 8,
            "block_N": 8,
            "block_K": 8,
            "num_stages": 1,
            "thread_num": 128,
            "policy": "GemmWarpPolicy.Square",
            "enable_rasteration": False,
        },
        module_name="pcc1_tilelang_local_matmul_nonroller_runtime",
    )


def _tilelang_local_matmul_static_roller_module() -> KernelModule:
    benchmark_path = Path.home() / "tilelang" / "benchmark" / "matmul" / "benchmark_matmul.py"
    if not benchmark_path.exists():
        pytest.fail(f"local TileLang benchmark reference not found: {benchmark_path}")
    return import_tilelang_source(
        benchmark_path.read_text(encoding="utf-8"),
        outer_function="matmul",
        prim_func="main",
        constants={
            "M": 5,
            "N": 7,
            "K": 3,
            "with_roller": True,
            "block_M": 8,
            "block_N": 8,
            "block_K": 8,
            "num_stages": 0,
            "thread_num": 32,
            "policy": (2, 1),
            "enable_rasteration": True,
        },
        module_name="pcc1_tilelang_local_matmul_static_roller_runtime",
    )


def _tilelang_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=8, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=8, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16, dtype="f32", device="metal:0"))
    return args


def _tilelang_local_metal_benchmark_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=3 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_output_staging_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_OUTPUT_STAGED_SOURCE,
        outer_function="matmul_output_staging",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 3, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_output_staging_runtime",
    )


def _tilelang_output_staging_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=3 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_output_staging_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(3))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(3)
    )
    return a, b


def _tilelang_output_staging_f16_transpose_b_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_OUTPUT_STAGED_F16_TRANSPOSE_B_SOURCE,
        outer_function="matmul_output_staging_f16_transpose_b",
        prim_func="gemm_kernel",
        constants={
            "M": 5,
            "N": 7,
            "K": 3,
            "block_M": 8,
            "block_N": 8,
            "block_K": 8,
            "num_stages": 0,
            "thread_num": 32,
            "block_rows": 2,
            "block_cols": 1,
            "enable_rasteration": True,
        },
        module_name="pcc1_tilelang_output_staging_f16_transpose_b_runtime",
    )


def _tilelang_output_staging_f16_transpose_b_policy_alias_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_OUTPUT_STAGED_F16_TRANSPOSE_B_POLICY_ALIAS_SOURCE,
        outer_function="matmul_output_staging_f16_transpose_b",
        prim_func="gemm_kernel",
        constants={
            "M": 5,
            "N": 7,
            "K": 3,
            "block_M": 8,
            "block_N": 8,
            "block_K": 8,
            "num_stages": 0,
            "thread_num": 32,
            "enable_rasteration": True,
        },
        module_name="pcc1_tilelang_output_staging_f16_transpose_b_policy_alias_runtime",
    )


def _tilelang_output_staging_f16_transpose_b_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=7 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 2, dtype="f16", device="metal:0"))
    return args


def _tilelang_output_staging_f16_transpose_b_inputs():
    a = tuple(
        tuple(_half_roundtrip((((i * 5) + kk + 1) % 11 - 5) / 7.0) for kk in range(3))
        for i in range(5)
    )
    b = tuple(
        tuple(_half_roundtrip((((j * 3) - kk + 2) % 13 - 6) / 5.0) for kk in range(3))
        for j in range(7)
    )
    return a, b


def _build_real_metallib_tilelang_output_staging_artifacts(tmp_path: Path):
    module = _tilelang_output_staging_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_output_staging_args(),
        tmp_path / "real_pcc1_tilelang_output_staging_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "TileLang output-staged GEMM metallib package did not produce "
            f"metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail("TileLang output-staged metallib package did not retain source")
    if "C[(row * 7u) + col] = (float)acc;" not in package.finalize.metal_source:
        pytest.fail("TileLang output-staged source lost global C store")
    if "C_shared" in package.finalize.metal_source:
        pytest.fail("TileLang output-staged source unexpectedly emitted C_shared")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "TileLang output-staged GEMM metallib bridge was not load-validated: "
            f"{package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("TileLang output-staged metallib bridge missing library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("TileLang output-staged launch plan missing metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_output_staging_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _tilelang_output_staging_inputs()
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    return package, native_runtime, a, b, expected


def _build_real_metallib_tilelang_output_staging_f16_transpose_b_artifacts(tmp_path: Path):
    module = _tilelang_output_staging_f16_transpose_b_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_output_staging_f16_transpose_b_args(),
        tmp_path / "real_pcc1_tilelang_output_staging_f16_transpose_b_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GEMM metallib package did not produce "
            f"metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail("TileLang output-staged f16 transpose_B metallib package did not retain source")
    if "device half* C [[buffer(2)]]" not in package.finalize.metal_source:
        pytest.fail("TileLang output-staged f16 transpose_B source lost f16 C buffer")
    if "swizzle_panel_size = 10u * swizzle_grid_x;" not in package.finalize.metal_source:
        pytest.fail("TileLang output-staged f16 transpose_B source lost enabled swizzle metadata")
    if "B[(b_row * 3u) + b_col]" not in package.finalize.metal_source:
        pytest.fail("TileLang output-staged f16 transpose_B source lost transposed B load")
    if "C[(row * 7u) + col] = (half)acc;" not in package.finalize.metal_source:
        pytest.fail("TileLang output-staged f16 transpose_B source lost half C store")
    if "C_shared" in package.finalize.metal_source:
        pytest.fail("TileLang output-staged f16 transpose_B source unexpectedly emitted C_shared")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GEMM metallib bridge was not load-validated: "
            f"{package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("TileLang output-staged f16 transpose_B metallib bridge missing library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("TileLang output-staged f16 transpose_B launch plan missing metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_output_staging_f16_transpose_b_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _tilelang_output_staging_f16_transpose_b_inputs()
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    return package, native_runtime, a, b, expected


def _build_real_metallib_tilelang_output_staging_f16_transpose_b_policy_alias_artifacts(
    tmp_path: Path,
):
    module = _tilelang_output_staging_f16_transpose_b_policy_alias_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_output_staging_f16_transpose_b_args(),
        tmp_path
        / "real_pcc1_tilelang_output_staging_f16_transpose_b_policy_alias_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias "
            f"metallib package did not produce metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias "
            "metallib package did not retain source"
        )
    if "device half* C [[buffer(2)]]" not in package.finalize.metal_source:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias source "
            "lost f16 C buffer"
        )
    if "swizzle_panel_size = 10u * swizzle_grid_x;" not in package.finalize.metal_source:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias source "
            "lost enabled swizzle metadata"
        )
    if "C[(row * 7u) + col] = (half)acc;" not in package.finalize.metal_source:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias source "
            "lost half C store"
        )
    if "C_shared" in package.finalize.metal_source:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias source "
            "unexpectedly emitted C_shared"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias "
            "metallib bridge was not load-validated: "
            f"{package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias "
            "metallib bridge missing library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "TileLang output-staged f16 transpose_B GemmWarpPolicy alias "
            "launch plan missing metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_tilelang_output_staging_f16_transpose_b_policy_alias_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _tilelang_output_staging_f16_transpose_b_inputs()
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    return package, native_runtime, a, b, expected


def _build_real_runtime_source_tilelang_gemm_artifacts(tmp_path: Path):
    module = _tilelang_gemm_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_gemm_args(),
        tmp_path / "real_pcc1_tilelang_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    return package, native_runtime, source_bridge, metal_source


def _tilelang_parallel_a_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_PARALLEL_A_COPY_SOURCE,
        outer_function="matmul_parallel_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_parallel_a_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs["parallel_vars"] == ["i", "kk"]
    assert b_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in b_copy.attrs
    assert c_copy.attrs == {}
    return module


def _tilelang_parallel_a_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_parallel_a_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_parallel_b_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_PARALLEL_B_COPY_SOURCE,
        outer_function="matmul_parallel_b_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_parallel_b_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in a_copy.attrs
    assert b_copy.attrs["parallel_vars"] == ["kk", "j"]
    assert c_copy.attrs == {}
    return module


def _tilelang_parallel_b_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_parallel_b_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_parallel_c_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_PARALLEL_C_COPY_SOURCE,
        outer_function="matmul_parallel_c_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_parallel_c_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in a_copy.attrs
    assert b_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in b_copy.attrs
    assert c_copy.attrs["parallel_vars"] == ["i", "j"]
    return module


def _tilelang_parallel_c_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_parallel_c_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_parallel_ab_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_PARALLEL_AB_COPY_SOURCE,
        outer_function="matmul_parallel_ab_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_parallel_ab_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs["parallel_vars"] == ["i", "kk"]
    assert b_copy.attrs["parallel_vars"] == ["kk", "j"]
    assert c_copy.attrs == {}
    return module


def _tilelang_parallel_ab_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_parallel_ab_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_parallel_abc_copy_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_PARALLEL_ABC_COPY_SOURCE,
        outer_function="matmul_parallel_abc_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_parallel_abc_copy_runtime",
    )


def _tilelang_parallel_abc_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_parallel_abc_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_vectorized_nonzero_serial_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_VECTORIZED_ABC_COPY_SOURCE,
        outer_function="matmul_vectorized_abc_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 24},
        module_name="pcc1_tilelang_vectorized_nonzero_serial_runtime",
    )


def _tilelang_vectorized_nonzero_serial_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 24 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=24 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_vectorized_nonzero_serial_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(24))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(24)
    )
    return a, b


def _build_real_runtime_source_tilelang_vectorized_nonzero_serial_artifacts(
    tmp_path: Path,
):
    module = _tilelang_vectorized_nonzero_serial_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_vectorized_nonzero_serial_args(),
        tmp_path / "real_pcc1_tilelang_vectorized_nonzero_serial_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_vectorized_nonzero_serial_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_vectorized_nonzero_serial_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_vectorized_nonzero_serial_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_vectorized_abc_copy_source() -> str:
    return _TILELANG_PCC1_VECTORIZED_ABC_COPY_SOURCE.replace(
        "for ko in T.serial(1, T.ceildiv(K, block_K)):",
        "for ko in T.serial(T.ceildiv(K, block_K)):",
    )


def _tilelang_vectorized_abc_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _tilelang_vectorized_abc_copy_source(),
        outer_function="matmul_vectorized_abc_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_vectorized_abc_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs["parallel_vars"] == ["i"]
    assert a_copy.attrs["vectorized_var"] == "kk"
    assert a_copy.attrs["vectorized_extent"] == 8
    assert b_copy.attrs["parallel_vars"] == ["kk"]
    assert b_copy.attrs["vectorized_var"] == "j"
    assert b_copy.attrs["vectorized_extent"] == 8
    assert c_copy.attrs["parallel_vars"] == ["i"]
    assert c_copy.attrs["vectorized_var"] == "j"
    assert c_copy.attrs["vectorized_extent"] == 8
    return module


def _tilelang_vectorized_a_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_VECTORIZED_A_COPY_SOURCE,
        outer_function="matmul_vectorized_a_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_vectorized_a_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs["parallel_vars"] == ["i"]
    assert a_copy.attrs["vectorized_var"] == "kk"
    assert a_copy.attrs["vectorized_extent"] == 8
    assert b_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in b_copy.attrs
    assert "vectorized_var" not in b_copy.attrs
    assert c_copy.attrs == {}
    return module


def _tilelang_vectorized_a_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_vectorized_a_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_vectorized_b_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_VECTORIZED_B_COPY_SOURCE,
        outer_function="matmul_vectorized_b_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_vectorized_b_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in a_copy.attrs
    assert "vectorized_var" not in a_copy.attrs
    assert b_copy.attrs["parallel_vars"] == ["kk"]
    assert b_copy.attrs["vectorized_var"] == "j"
    assert b_copy.attrs["vectorized_extent"] == 8
    assert c_copy.attrs == {}
    return module


def _tilelang_vectorized_b_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_vectorized_b_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_vectorized_c_copy_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_VECTORIZED_C_COPY_SOURCE,
        outer_function="matmul_vectorized_c_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_vectorized_c_copy_runtime",
    )
    body = module.funcs[0].body
    a_copy = next(
        op for op in body if op.op == "copy" and op.args == ("A", "A_shared")
    )
    b_copy = next(
        op for op in body if op.op == "copy" and op.args == ("B", "B_shared")
    )
    c_copy = next(
        op for op in body if op.op == "copy" and op.args == ("C_local", "C")
    )
    assert a_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in a_copy.attrs
    assert "vectorized_var" not in a_copy.attrs
    assert b_copy.attrs.get("serial_extent") == 2
    assert "parallel_vars" not in b_copy.attrs
    assert "vectorized_var" not in b_copy.attrs
    assert c_copy.attrs["parallel_vars"] == ["i"]
    assert c_copy.attrs["vectorized_var"] == "j"
    assert c_copy.attrs["vectorized_extent"] == 8
    return module


def _tilelang_vectorized_c_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_vectorized_c_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_vectorized_abc_copy_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_vectorized_abc_copy_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_vectorized_annotations_source() -> str:
    return _TILELANG_PCC1_VECTORIZED_ABC_COPY_SOURCE.replace(
        "for ko in T.serial(1, T.ceildiv(K, block_K)):",
        "for ko in T.serial(T.ceildiv(K, block_K)):",
    ).replace(
        "for kk in T.vectorized(block_K):",
        'for kk in T.vectorized(0, block_K, annotations={"pragma_unroll": True}):',
        1,
    )


def _tilelang_vectorized_annotations_module() -> KernelModule:
    module = import_tilelang_source(
        _tilelang_vectorized_annotations_source(),
        outer_function="matmul_vectorized_abc_copy",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_vectorized_annotations_runtime",
    )
    a_copy = next(
        op for op in module.funcs[0].body
        if op.op == "copy" and op.args[1] == "A_shared"
    )
    assert a_copy.attrs["vectorized_annotations"] == {"pragma_unroll": True}
    return module


def _tilelang_vectorized_annotations_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_vectorized_annotations_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _build_real_runtime_source_tilelang_vectorized_annotations_artifacts(
    tmp_path: Path,
):
    module = _tilelang_vectorized_annotations_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_vectorized_annotations_args(),
        tmp_path / "real_pcc1_tilelang_vectorized_annotations_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "for (uint ko = 0u; ko < 2u; ++ko)" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_vectorized_annotations_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_vectorized_annotations_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_vectorized_annotations_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_splitk_atomic_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_SPLITK_ATOMIC_SOURCE,
        outer_function="matmul_splitk_atomic",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_splitk_atomic_runtime",
    )


def _tilelang_splitk_atomic_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_splitk_atomic_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(16)
    )
    return a, b


def _tilelang_splitk_atomic_vectorized_c_module() -> KernelModule:
    module = import_tilelang_source(
        _TILELANG_PCC1_SPLITK_ATOMIC_VECTORIZED_C_SOURCE,
        outer_function="matmul_splitk_atomic_vectorized_c",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 16},
        module_name="pcc1_tilelang_splitk_atomic_vectorized_c_runtime",
    )
    body = module.funcs[0].body
    atomic = next(
        op
        for op in body
        if op.op == "atomic_add" and op.args == ("C", "C_local")
    )
    assert atomic.attrs["parallel_vars"] == ["i"]
    assert atomic.attrs["parallel_extents"] == [8]
    assert atomic.attrs["vectorized_var"] == "j"
    assert atomic.attrs["vectorized_extent"] == 8
    return module


def _tilelang_splitk_atomic_vectorized_c_args() -> PccPackedArgs:
    return _tilelang_splitk_atomic_args()


def _tilelang_splitk_atomic_vectorized_c_inputs():
    return _tilelang_splitk_atomic_inputs()


def _tilelang_splitk_atomic_ceildiv_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_SPLITK_ATOMIC_CEILDIV_SOURCE,
        outer_function="matmul_splitk_atomic",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 17, "split_k": 4},
        module_name="pcc1_tilelang_splitk_atomic_ceildiv_runtime",
    )


def _tilelang_splitk_atomic_floor_plus_one_ceildiv_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_SPLITK_ATOMIC_FLOOR_PLUS_ONE_CEILDIV_SOURCE,
        outer_function="matmul_splitk_atomic",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 17, "split_k": 4},
        module_name="pcc1_tilelang_splitk_atomic_floor_plus_one_ceildiv_runtime",
    )


def _tilelang_splitk_atomic_ceildiv_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 17 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=17 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_splitk_atomic_ceildiv_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(17))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(17)
    )
    return a, b


def _build_real_runtime_source_tilelang_splitk_atomic_artifacts(tmp_path: Path):
    module = _tilelang_splitk_atomic_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_splitk_atomic_args(),
        tmp_path / "real_pcc1_tilelang_splitk_atomic_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint split_k_index = tgid.z;" in metal_source
    assert "atomic_fetch_add_explicit" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_splitk_atomic_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_splitk_atomic_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_splitk_atomic_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_tilelang_splitk_atomic_ceildiv_artifacts(
    tmp_path: Path,
):
    module = _tilelang_splitk_atomic_ceildiv_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_splitk_atomic_ceildiv_args(),
        tmp_path / "real_pcc1_tilelang_splitk_atomic_ceildiv_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint split_k0 = split_k_index * 5u;" in metal_source
    assert "uint split_k_end = min(split_k0 + 5u, 17u);" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_splitk_atomic_ceildiv_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_splitk_atomic_ceildiv_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_splitk_atomic_ceildiv_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_metallib_tilelang_splitk_floor_plus_one_ceildiv_artifacts(
    tmp_path: Path,
):
    module = _tilelang_splitk_atomic_floor_plus_one_ceildiv_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_splitk_atomic_ceildiv_args(),
        tmp_path / "real_pcc1_tilelang_splitk_floor_plus_one_ceildiv_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv metallib package did not "
            f"produce metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv metallib package did not retain source"
        )
    if "device atomic_float* C [[buffer(2)]]" not in package.finalize.metal_source:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv source lost atomic output pointer"
        )
    if "uint split_k0 = split_k_index * 5u;" not in package.finalize.metal_source:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv source lost split_k0 computation"
        )
    if "uint split_k_end = min(split_k0 + 5u, 17u);" not in package.finalize.metal_source:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv source lost split_k_end guard"
        )
    if "atomic_fetch_add_explicit" not in package.finalize.metal_source:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv source lost atomic accumulation"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv bridge missing library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "TileLang split-K floor-plus-one ceildiv launch plan missing metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_tilelang_splitk_floor_plus_one_ceildiv_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _tilelang_splitk_atomic_ceildiv_inputs()
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    return package, native_runtime, a, b, expected


def _tilelang_transpose_a_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_TRANSPOSE_A_SOURCE,
        outer_function="matmul_transpose_a",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 3},
        module_name="pcc1_tilelang_transpose_a_runtime",
    )


def _tilelang_transpose_a_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=3 * 5 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=3 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_transpose_a_inputs():
    a = tuple(
        tuple(float(((kk * 3) + i) % 7 - 3) for i in range(5))
        for kk in range(3)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(3)
    )
    return a, b


def _tilelang_transpose_b_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_TRANSPOSE_B_SOURCE,
        outer_function="matmul_transpose_b",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 3},
        module_name="pcc1_tilelang_transpose_b_runtime",
    )


def _tilelang_transpose_b_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=7 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_transpose_b_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(3))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((j * 2) - kk) % 9 - 4) for kk in range(3))
        for j in range(7)
    )
    return a, b


def _tilelang_transpose_ab_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_TRANSPOSE_AB_SOURCE,
        outer_function="matmul_transpose_ab",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 3},
        module_name="pcc1_tilelang_transpose_ab_runtime",
    )


def _tilelang_transpose_ab_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=3 * 5 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=7 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_transpose_ab_inputs():
    a = tuple(
        tuple(float(((kk * 3) + i) % 7 - 3) for i in range(5))
        for kk in range(3)
    )
    b = tuple(
        tuple(float(((j * 2) - kk) % 9 - 4) for kk in range(3))
        for j in range(7)
    )
    return a, b


def _build_real_runtime_source_tilelang_transpose_ab_artifacts(tmp_path: Path):
    module = _tilelang_transpose_ab_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_transpose_ab_args(),
        tmp_path / "real_pcc1_tilelang_transpose_ab_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "A_shared[((kk * 8u) + local_m)]" in metal_source
    assert "B_shared[((local_n * 8u) + kk)]" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_transpose_ab_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_transpose_ab_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_transpose_ab_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_swizzled_padded_annotate_layout_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_SWIZZLED_PADDED_ANNOTATE_LAYOUT_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 3, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_swizzled_padded_annotate_layout_runtime",
    )


def _tilelang_swizzled_padded_annotate_layout_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 3 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=3 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_swizzled_padded_annotate_layout_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(3))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(3)
    )
    return a, b


def _build_real_runtime_source_tilelang_swizzled_padded_annotate_layout_artifacts(
    tmp_path: Path,
):
    module = _tilelang_swizzled_padded_annotate_layout_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_swizzled_padded_annotate_layout_args(),
        tmp_path / "real_pcc1_tilelang_swizzled_padded_annotate_layout_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "uint a_shared_idx = ((a_local_m * 8u) + a_local_k);" in metal_source
    assert "uint b_shared_idx = ((b_local_k * 8u) + b_local_n);" in metal_source
    assert "A_shared[a_shared_idx]" in metal_source
    assert "B_shared[b_shared_idx]" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_swizzled_padded_annotate_layout_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_swizzled_padded_annotate_layout_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_swizzled_padded_annotate_layout_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_enabled_swizzle_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_ENABLED_SWIZZLE_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 17, "N": 19, "K": 16, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_enabled_swizzle_runtime",
    )


def _tilelang_enabled_swizzle_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=17 * 16 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=16 * 19 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=17 * 19 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_enabled_swizzle_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(16))
        for i in range(17)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(19))
        for kk in range(16)
    )
    return a, b


def _build_real_runtime_source_tilelang_enabled_swizzle_artifacts(tmp_path: Path):
    module = _tilelang_enabled_swizzle_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_enabled_swizzle_args(),
        tmp_path / "real_pcc1_tilelang_enabled_swizzle_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "uint swizzle_grid_x = 3u;" in metal_source
    assert "uint swizzle_grid_y = 3u;" in metal_source
    assert "uint swizzle_panel_size = 2u * swizzle_grid_x;" in metal_source
    assert "uint tile_col0 = tile_gid_x * 8u;" in metal_source
    assert "uint tile_row0 = tile_gid_y * 8u;" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_enabled_swizzle_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_enabled_swizzle_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_enabled_swizzle_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_nonzero_start_pipelined_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_NONZERO_START_PIPELINED_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 24, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_nonzero_start_pipelined_runtime",
    )


def _tilelang_nonzero_start_pipelined_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 24 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=24 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_nonzero_start_pipelined_inputs():
    return _tilelang_vectorized_nonzero_serial_inputs()


def _build_real_runtime_source_tilelang_nonzero_start_pipelined_artifacts(
    tmp_path: Path,
):
    module = _tilelang_nonzero_start_pipelined_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_nonzero_start_pipelined_args(),
        tmp_path / "real_pcc1_tilelang_nonzero_start_pipelined_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "for (uint ko = 1u; ko < 3u; ++ko)" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_nonzero_start_pipelined_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_nonzero_start_pipelined_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_nonzero_start_pipelined_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_step_serial_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_STEP_SERIAL_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 32, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_step_serial_runtime",
    )


def _tilelang_step_serial_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 32 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=32 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_step_serial_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(32))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(32)
    )
    return a, b


def _build_real_runtime_source_tilelang_step_serial_artifacts(
    tmp_path: Path,
):
    module = _tilelang_step_serial_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_step_serial_args(),
        tmp_path / "real_pcc1_tilelang_step_serial_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "for (uint ko = 0u; ko < 4u; ko += 2u)" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_step_serial_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_step_serial_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_step_serial_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_step_pipelined_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_STEP_PIPELINED_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 32, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_step_pipelined_runtime",
    )


def _build_real_runtime_source_tilelang_step_pipelined_artifacts(
    tmp_path: Path,
):
    module = _tilelang_step_pipelined_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_step_serial_args(),
        tmp_path / "real_pcc1_tilelang_step_pipelined_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "for (uint ko = 0u; ko < 4u; ko += 2u)" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_step_pipelined_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_step_pipelined_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_step_serial_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_nonzero_step_pipelined_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_NONZERO_STEP_PIPELINED_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 40, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_nonzero_step_pipelined_runtime",
    )


def _tilelang_nonzero_step_pipelined_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=5 * 40 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=40 * 7 * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=5 * 7 * 4, dtype="f32", device="metal:0"))
    return args


def _tilelang_nonzero_step_pipelined_inputs():
    a = tuple(
        tuple(float(((i * 3) + kk) % 7 - 3) for kk in range(40))
        for i in range(5)
    )
    b = tuple(
        tuple(float(((kk * 2) - j) % 9 - 4) for j in range(7))
        for kk in range(40)
    )
    return a, b


def _build_real_runtime_source_tilelang_nonzero_step_pipelined_artifacts(
    tmp_path: Path,
):
    module = _tilelang_nonzero_step_pipelined_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_nonzero_step_pipelined_args(),
        tmp_path / "real_pcc1_tilelang_nonzero_step_pipelined_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "for (uint ko = 1u; ko < 5u; ko += 2u)" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_nonzero_step_pipelined_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_nonzero_step_pipelined_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_nonzero_step_pipelined_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _tilelang_nonzero_step_serial_module() -> KernelModule:
    return import_tilelang_source(
        _TILELANG_PCC1_NONZERO_STEP_SERIAL_SOURCE,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": 5, "N": 7, "K": 40, "block_M": 8, "block_N": 8, "block_K": 8},
        module_name="pcc1_tilelang_nonzero_step_serial_runtime",
    )


def _build_real_runtime_source_tilelang_nonzero_step_serial_artifacts(
    tmp_path: Path,
):
    module = _tilelang_nonzero_step_serial_module()
    package = build_metal_kernel_package(
        module,
        _tilelang_nonzero_step_pipelined_args(),
        tmp_path / "real_pcc1_tilelang_nonzero_step_serial_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_source(module)
    assert "for (uint ko = 1u; ko < 5u; ko += 2u)" in metal_source

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_tilelang_nonzero_step_serial_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_tilelang_nonzero_step_serial_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _tilelang_nonzero_step_pipelined_inputs()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _simdgroup_pcc1_micro_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(8, 8),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(8, 8),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(8, 8),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(8, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(8, 8),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(1, 1),
        threads=32,
    )
    return KernelModule("pcc1_simdgroup_micro_gemm_mod", funcs=(func,))


def _simdgroup_pcc1_two_n_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(8, 8),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(8, 16),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(8, 16),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(8, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(1, 1),
        threads=64,
    )
    return KernelModule("pcc1_simdgroup_two_n_gemm_mod", funcs=(func,))


def _simdgroup_pcc1_two_m_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(16, 8),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(8, 8),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(16, 8),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(8, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 8),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(1, 1),
        threads=64,
    )
    return KernelModule("pcc1_simdgroup_two_m_gemm_mod", funcs=(func,))


def _simdgroup_pcc1_four_2d_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(16, 8),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(8, 16),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(16, 16),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(1, 1),
        threads=128,
    )
    return KernelModule("pcc1_simdgroup_four_2d_gemm_mod", funcs=(func,))


def _simdgroup_pcc1_four_2d_transpose_ab_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(8, 16),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(16, 8),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(16, 16),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp(
                "gemm",
                ("A_shared", "B_shared", "C_local"),
                {
                    "pipeline_extent": 1,
                    "num_stages": 0,
                    "transpose_A": True,
                    "transpose_B": True,
                },
            ),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(1, 1),
        threads=128,
    )
    return KernelModule("pcc1_simdgroup_four_2d_transpose_ab_gemm_mod", funcs=(func,))


def _simdgroup_pcc1_four_2d_edge_tail_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(15, 9),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(9, 15),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(15, 15),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 2, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 2, "num_stages": 0}),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local"), {"pipeline_extent": 2, "num_stages": 0}),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(1, 1),
        threads=128,
    )
    return KernelModule("pcc1_simdgroup_four_2d_edge_tail_gemm_mod", funcs=(func,))


def _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(9, 15),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(15, 9),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(15, 15),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 2, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 2, "num_stages": 0}),
            KernelOp(
                "gemm",
                ("A_shared", "B_shared", "C_local"),
                {
                    "pipeline_extent": 2,
                    "num_stages": 0,
                    "transpose_A": True,
                    "transpose_B": True,
                },
            ),
            KernelOp("copy", ("C_local", "C")),
        ),
        grid=(1, 1),
        threads=128,
    )
    return KernelModule(
        "pcc1_simdgroup_four_2d_transpose_ab_edge_tail_gemm_mod",
        funcs=(func,),
    )


def _simdgroup_pcc1_four_2d_splitk_atomic_gemm_module() -> KernelModule:
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(16, 16),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(16, 16),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(16, 16),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), {"pipeline_extent": 2, "num_stages": 0}),
            KernelOp("copy", ("B", "B_shared"), {"pipeline_extent": 2, "num_stages": 0}),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("atomic_add", ("C", "C_local")),
        ),
        grid=(1, 1, 2),
        threads=128,
    )
    return KernelModule(
        "pcc1_simdgroup_four_2d_splitk_atomic_gemm_mod",
        funcs=(func,),
    )


def _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_gemm_module() -> KernelModule:
    split_copy_attrs = {
        "pipeline_extent": 3,
        "num_stages": 0,
        "split_k_span_mode": "ceildiv",
        "split_k_span": 5,
        "split_k_axis_var": "bz",
    }
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(15, 17),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(17, 15),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(15, 15),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), split_copy_attrs),
            KernelOp("copy", ("B", "B_shared"), split_copy_attrs),
            KernelOp("gemm", ("A_shared", "B_shared", "C_local"), {"pipeline_extent": 1, "num_stages": 0}),
            KernelOp("atomic_add", ("C", "C_local")),
        ),
        grid=(1, 1, 4),
        threads=128,
    )
    return KernelModule(
        "pcc1_simdgroup_four_2d_splitk_atomic_edge_tail_gemm_mod",
        funcs=(func,),
    )


def _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_module() -> KernelModule:
    split_copy_attrs = {
        "pipeline_extent": 3,
        "num_stages": 0,
        "split_k_span_mode": "ceildiv",
        "split_k_span": 5,
        "split_k_axis_var": "bz",
    }
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(17, 15),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(15, 17),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(15, 15),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(16, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 16),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), split_copy_attrs),
            KernelOp("copy", ("B", "B_shared"), split_copy_attrs),
            KernelOp(
                "gemm",
                ("A_shared", "B_shared", "C_local"),
                {
                    "pipeline_extent": 1,
                    "num_stages": 0,
                    "transpose_A": True,
                    "transpose_B": True,
                },
            ),
            KernelOp("atomic_add", ("C", "C_local")),
        ),
        grid=(1, 1, 4),
        threads=128,
    )
    return KernelModule(
        "pcc1_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_mod",
        funcs=(func,),
    )


def _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_module() -> KernelModule:
    split_copy_attrs = {
        "pipeline_extent": 3,
        "num_stages": 0,
        "split_k_span_mode": "ceildiv",
        "split_k_span": 5,
        "split_k_axis_var": "bz",
    }
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(17, 15),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(31, 17),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(15, 31),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 16),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(32, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(16, 32),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), split_copy_attrs),
            KernelOp("copy", ("B", "B_shared"), split_copy_attrs),
            KernelOp(
                "gemm",
                ("A_shared", "B_shared", "C_local"),
                {
                    "pipeline_extent": 1,
                    "num_stages": 0,
                    "transpose_A": True,
                    "transpose_B": True,
                },
            ),
            KernelOp("atomic_add", ("C", "C_local")),
        ),
        grid=(1, 1, 4),
        threads=256,
    )
    return KernelModule(
        "pcc1_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_mod",
        funcs=(func,),
    )


def _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_module() -> KernelModule:
    split_copy_attrs = {
        "pipeline_extent": 3,
        "num_stages": 0,
        "split_k_span_mode": "ceildiv",
        "split_k_span": 5,
        "split_k_axis_var": "bz",
    }
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(17, 31),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(31, 17),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(31, 31),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 32),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(32, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(32, 32),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), split_copy_attrs),
            KernelOp("copy", ("B", "B_shared"), split_copy_attrs),
            KernelOp(
                "gemm",
                ("A_shared", "B_shared", "C_local"),
                {
                    "pipeline_extent": 1,
                    "num_stages": 0,
                    "transpose_A": True,
                    "transpose_B": True,
                },
            ),
            KernelOp("atomic_add", ("C", "C_local")),
        ),
        grid=(1, 1, 4),
        threads=512,
    )
    return KernelModule(
        "pcc1_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_mod",
        funcs=(func,),
    )


def _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_module() -> KernelModule:
    split_copy_attrs = {
        "pipeline_extent": 3,
        "num_stages": 0,
        "split_k_span_mode": "ceildiv",
        "split_k_span": 5,
        "split_k_axis_var": "bz",
    }
    func = KernelFunc(
        name="gemm_kernel",
        params=(
            BufferParam(
                "A",
                ScalarType.F16,
                rank=2,
                shape=(17, 31),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "B",
                ScalarType.F16,
                rank=2,
                shape=(63, 17),
                scope=MemoryScope.GLOBAL,
            ),
            BufferParam(
                "C",
                ScalarType.F32,
                rank=2,
                shape=(31, 63),
                scope=MemoryScope.GLOBAL,
            ),
        ),
        locals=(
            LocalBuffer(
                "A_shared",
                ScalarType.F16,
                shape=(8, 32),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "B_shared",
                ScalarType.F16,
                shape=(64, 8),
                scope=MemoryScope.SHARED,
                layout=Layout.TILE,
            ),
            LocalBuffer(
                "C_local",
                ScalarType.F32,
                shape=(32, 64),
                scope=MemoryScope.FRAGMENT,
                layout=Layout.TILE,
            ),
        ),
        body=(
            KernelOp("fill", ("C_local",), {"value": 0}),
            KernelOp("copy", ("A", "A_shared"), split_copy_attrs),
            KernelOp("copy", ("B", "B_shared"), split_copy_attrs),
            KernelOp(
                "gemm",
                ("A_shared", "B_shared", "C_local"),
                {
                    "pipeline_extent": 1,
                    "num_stages": 0,
                    "transpose_A": True,
                    "transpose_B": True,
                },
            ),
            KernelOp("atomic_add", ("C", "C_local")),
        ),
        grid=(1, 1, 4),
        threads=1024,
    )
    return KernelModule(
        "pcc1_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_mod",
        funcs=(func,),
    )


def _simdgroup_pcc1_micro_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=128, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=128, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=256, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_input_matrices():
    a = tuple(
        tuple(float(((row + col) % 5) + 1) for col in range(8))
        for row in range(8)
    )
    b = tuple(
        tuple(float(((row * 2 + col) % 7) + 1) for col in range(8))
        for row in range(8)
    )
    return a, b


def _simdgroup_pcc1_two_n_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=128, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=256, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=512, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_two_n_input_matrices():
    a = tuple(
        tuple(float(((row + col) % 5) + 1) for col in range(8))
        for row in range(8)
    )
    b = tuple(
        tuple(float(((row * 2 + col) % 7) + 1) for col in range(16))
        for row in range(8)
    )
    return a, b


def _simdgroup_pcc1_two_m_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=256, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=128, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=512, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_two_m_input_matrices():
    a = tuple(
        tuple(float(((row + col) % 5) + 1) for col in range(8))
        for row in range(16)
    )
    b = tuple(
        tuple(float(((row * 2 + col) % 7) + 1) for col in range(8))
        for row in range(8)
    )
    return a, b


def _simdgroup_pcc1_four_2d_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=256, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=256, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=1024, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_four_2d_input_matrices():
    a = tuple(
        tuple(float(((row + col) % 5) + 1) for col in range(8))
        for row in range(16)
    )
    b = tuple(
        tuple(float(((row * 2 + col) % 7) + 1) for col in range(16))
        for row in range(8)
    )
    return a, b


def _simdgroup_pcc1_four_2d_transpose_ab_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=256, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=256, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=1024, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_four_2d_transpose_ab_input_matrices():
    a = tuple(
        tuple(float(((row * 3) + col) % 7 - 3) for col in range(16))
        for row in range(8)
    )
    b = tuple(
        tuple(float(((row * 2) - col) % 9 - 4) for col in range(8))
        for row in range(16)
    )
    return a, b


def _simdgroup_pcc1_four_2d_edge_tail_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=270, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=270, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=900, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_four_2d_edge_tail_input_matrices():
    a = tuple(
        tuple(float((row + col) % 5 - 2) for col in range(9))
        for row in range(15)
    )
    b = tuple(
        tuple(float((row * 2 + col) % 7 - 3) for col in range(15))
        for row in range(9)
    )
    return a, b


def _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=270, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=270, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=900, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_input_matrices():
    a = tuple(
        tuple(float(((row * 3) + col) % 7 - 3) for col in range(15))
        for row in range(9)
    )
    b = tuple(
        tuple(float(((row * 2) - col) % 9 - 4) for col in range(9))
        for row in range(15)
    )
    return a, b


def _simdgroup_pcc1_four_2d_splitk_atomic_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=512, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=512, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=1024, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_four_2d_splitk_atomic_input_matrices():
    a = tuple(
        tuple(float((row + col) % 5 - 2) for col in range(16))
        for row in range(16)
    )
    b = tuple(
        tuple(float((row * 2 + col) % 7 - 3) for col in range(16))
        for row in range(16)
    )
    return a, b


def _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=510, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=510, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=900, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_input_matrices():
    a = tuple(
        tuple(float((row + col) % 5 - 2) for col in range(17))
        for row in range(15)
    )
    b = tuple(
        tuple(float((row * 2 + col) % 7 - 3) for col in range(15))
        for row in range(17)
    )
    return a, b


def _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=510, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=510, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=900, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_input_matrices():
    a = tuple(
        tuple(float(((row * 3) + col) % 7 - 3) for col in range(15))
        for row in range(17)
    )
    b = tuple(
        tuple(float(((row * 2) - col) % 9 - 4) for col in range(17))
        for row in range(15)
    )
    return a, b


def _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=510, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=1054, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=1860, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_input_matrices():
    a = tuple(
        tuple(float(((row * 3) + col) % 7 - 3) for col in range(15))
        for row in range(17)
    )
    b = tuple(
        tuple(float(((row * 2) - col) % 9 - 4) for col in range(17))
        for row in range(31)
    )
    return a, b


def _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=1054, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=1054, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=3844, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_input_matrices():
    a = tuple(
        tuple(float(((row * 3) + col) % 7 - 3) for col in range(31))
        for row in range(17)
    )
    b = tuple(
        tuple(float(((row * 2) - col) % 9 - 4) for col in range(17))
        for row in range(31)
    )
    return a, b


def _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=1054, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=2142, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=7812, dtype="f32", device="metal:0"))
    return args


def _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_input_matrices():
    a = tuple(
        tuple(float(((row * 3) + col) % 7 - 3) for col in range(31))
        for row in range(17)
    )
    b = tuple(
        tuple(float(((row * 2) - col) % 9 - 4) for col in range(17))
        for row in range(63)
    )
    return a, b


def _half_bits(value: float) -> int:
    return int.from_bytes(struct.pack("<e", float(value)), "little")


def _half_roundtrip(value: float) -> float:
    return struct.unpack("<e", struct.pack("<e", float(value)))[0]


def _signed_i32_bits(value: int) -> int:
    value = value & 0xFFFFFFFF
    if value >= 0x80000000:
        return value - 0x100000000
    return value


def _f32_bits(value: float) -> int:
    return int.from_bytes(struct.pack("<f", float(value)), "little", signed=True)


def _pcc1_store_f16_matrix_lines(ptr_name: str, matrix, *, indent: str) -> str:
    flat = [value for row in matrix for value in row]
    if len(flat) % 2 != 0:
        raise AssertionError("pcc1 f16 matrix payload must have an even element count")
    lines: list[str] = []
    for index in range(0, len(flat), 2):
        packed = _half_bits(flat[index]) | (_half_bits(flat[index + 1]) << 16)
        lines.append(f"{indent}store_i32({ptr_name}, {index * 2}, {_signed_i32_bits(packed)})")
    return "\n".join(lines)


def _pcc1_store_f16_matrix_byte_lines(ptr_name: str, matrix, *, indent: str) -> str:
    lines: list[str] = []
    offset = 0
    for row in matrix:
        for value in row:
            raw = struct.pack("<e", float(value))
            lines.append(f"{indent}store_i8({ptr_name}, {offset}, {raw[0]})")
            lines.append(f"{indent}store_i8({ptr_name}, {offset + 1}, {raw[1]})")
            offset += 2
    return "\n".join(lines)


def _pcc1_assert_f32_matrix_lines(
    ptr_name: str,
    matrix,
    *,
    indent: str,
    first_error_code: int,
) -> str:
    flat = [value for row in matrix for value in row]
    lines: list[str] = []
    for index, value in enumerate(flat):
        lines.extend(
            (
                f"{indent}if load_i32({ptr_name}, {index * 4}) != {_f32_bits(value)}:",
                f"{indent}    print({first_error_code + index})",
                f"{indent}    return",
            )
        )
    return "\n".join(lines)


def _pcc1_assert_f16_matrix_byte_lines(
    ptr_name: str,
    matrix,
    *,
    indent: str,
    first_error_code: int,
) -> str:
    lines: list[str] = []
    offset = 0
    error_code = first_error_code
    for row in matrix:
        for value in row:
            raw = struct.pack("<e", float(value))
            lines.extend(
                (
                    f"{indent}if (load_i8({ptr_name}, {offset}) & 255) != {raw[0]}:",
                    f"{indent}    print({error_code})",
                    f"{indent}    return",
                    f"{indent}if (load_i8({ptr_name}, {offset + 1}) & 255) != {raw[1]}:",
                    f"{indent}    print({error_code + 1})",
                    f"{indent}    return",
                )
            )
            offset += 2
            error_code += 2
    return "\n".join(lines)


def _pcc1_zero_i32_payload_lines(ptr_name: str, nbytes: int, *, indent: str) -> str:
    if nbytes % 4 != 0:
        raise AssertionError("pcc1 zero payload helper expects a 4-byte multiple")
    return "\n".join(
        f"{indent}store_i32({ptr_name}, {offset}, 0)"
        for offset in range(0, nbytes, 4)
    )


def _build_real_runtime_source_simdgroup_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_micro_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_micro_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_micro_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_micro_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            f"simdgroup metallib package did not produce metallib: {package.to_dict()}"
        )
    if package.finalize.metal_source is None:
        pytest.fail("simdgroup metallib package did not retain Metal source")
    if "simdgroup_multiply_accumulate" not in package.finalize.metal_source:
        pytest.fail("simdgroup metallib package did not use simdgroup source")
    if not package.bridge_library_load_validated:
        pytest.fail(
            f"simdgroup metallib bridge was not load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("simdgroup metallib bridge did not produce a library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("simdgroup launch plan did not carry a metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_two_n_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_two_n_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_two_n_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_two_n_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "two-N simdgroup metallib package did not produce metallib: "
            f"{package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail("two-N simdgroup metallib package did not retain Metal source")
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail("two-N simdgroup metallib package did not use simdgroup source")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("two-N simdgroup source missing simdgroup index")
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail("two-N simdgroup source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail("two-N simdgroup source missing N tile split")
    if (
        "simdgroup_store(C_frag[0], C + ((tgid.y * 8u) * 16u) + "
        "((tgid.x * 16u) + (simdgroup_tile_n * 8u)), 16u, 0, false);"
        not in metal_source
    ):
        pytest.fail("two-N simdgroup source missing N-stride store")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "two-N simdgroup metallib bridge was not load-validated: "
            f"{package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("two-N simdgroup metallib bridge did not produce a library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("two-N simdgroup launch plan did not carry a metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_two_n_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_two_n_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_two_m_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_two_m_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_two_m_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_two_m_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "two-M simdgroup metallib package did not produce metallib: "
            f"{package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail("two-M simdgroup metallib package did not retain Metal source")
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail("two-M simdgroup metallib package did not use simdgroup source")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("two-M simdgroup source missing simdgroup index")
    if "uint simdgroup_tile_m = simdgroup_idx / 1u;" not in metal_source:
        pytest.fail("two-M simdgroup source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 1u;" not in metal_source:
        pytest.fail("two-M simdgroup source missing N tile split")
    if (
        "simdgroup_store(C_frag[0], C + (((tgid.y * 16u) + "
        "(simdgroup_tile_m * 8u)) * 8u) + (tgid.x * 8u), 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("two-M simdgroup source missing M-stride store")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "two-M simdgroup metallib bridge was not load-validated: "
            f"{package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("two-M simdgroup metallib bridge did not produce a library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("two-M simdgroup launch plan did not carry a metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_two_m_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_two_m_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_four_2d_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_four_2d_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "four-2D simdgroup metallib package did not produce metallib: "
            f"{package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail("four-2D simdgroup metallib package did not retain Metal source")
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail("four-2D simdgroup metallib package did not use simdgroup source")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("four-2D simdgroup source missing simdgroup index")
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail("four-2D simdgroup source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail("four-2D simdgroup source missing N tile split")
    if (
        "simdgroup_store(C_frag[0], C + (((tgid.y * 16u) + "
        "(simdgroup_tile_m * 8u)) * 16u) + ((tgid.x * 16u) + "
        "(simdgroup_tile_n * 8u)), 16u, 0, false);"
        not in metal_source
    ):
        pytest.fail("four-2D simdgroup source missing 2D-stride store")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "four-2D simdgroup metallib bridge was not load-validated: "
            f"{package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail("four-2D simdgroup metallib bridge did not produce a library path")
    if package.launch_plan.metallib_path is None:
        pytest.fail("four-2D simdgroup launch plan did not carry a metallib path")

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_four_2d_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_four_2d_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_edge_tail_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_edge_tail_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "four-2D edge/tail simdgroup metallib package did not produce "
            f"metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "four-2D edge/tail simdgroup metallib package did not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "four-2D edge/tail simdgroup metallib package did not use "
            "simdgroup source"
        )
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing simdgroup index")
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing simdgroup lane")
    if "threadgroup half A_tile[256];" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing A staging tile")
    if "threadgroup half B_tile[256];" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing B staging tile")
    if "threadgroup float C_tile[256];" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing C staging tile")
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing N tile split")
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing tile offset")
    if (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < 9u) ? A[(global_m * 9u) + global_k] : half(0.0);"
        not in metal_source
    ):
        pytest.fail("four-2D edge/tail simdgroup source missing guarded A load")
    if (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < 9u && "
        "global_n < 15u) ? B[(global_k * 15u) + global_n] : half(0.0);"
        not in metal_source
    ):
        pytest.fail("four-2D edge/tail simdgroup source missing guarded B load")
    if (
        "simdgroup_store(C_frag[0], C_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("four-2D edge/tail simdgroup source missing staged store")
    if "if (row < 15u && col < 15u) {" not in metal_source:
        pytest.fail("four-2D edge/tail simdgroup source missing writeback guard")
    if (
        "C[(row * 15u) + col] = C_tile[simdgroup_tile_offset + c_linear];"
        not in metal_source
    ):
        pytest.fail("four-2D edge/tail simdgroup source missing guarded writeback")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "four-2D edge/tail simdgroup metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "four-2D edge/tail simdgroup metallib bridge did not produce a "
            "library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "four-2D edge/tail simdgroup launch plan did not carry a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_four_2d_edge_tail_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_four_2d_transpose_ab_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_transpose_ab_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_transpose_ab_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_transpose_ab_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "four-2D transpose_AB simdgroup metallib package did not produce "
            f"metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "four-2D transpose_AB simdgroup metallib package did not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB simdgroup metallib package did not use "
            "simdgroup source"
        )
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("four-2D transpose_AB source missing simdgroup index")
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail("four-2D transpose_AB source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail("four-2D transpose_AB source missing N tile split")
    if (
        "simdgroup_load(A_frag[0], A + ((ko * 8u) * 16u) + "
        "((tgid.y * 16u) + (simdgroup_tile_m * 8u)), 16u, 0, true);"
        not in metal_source
    ):
        pytest.fail("four-2D transpose_AB source missing transposed A load")
    if (
        "simdgroup_load(B_frag[0], B + (((tgid.x * 16u) + "
        "(simdgroup_tile_n * 8u)) * 8u) + (ko * 8u), 8u, 0, true);"
        not in metal_source
    ):
        pytest.fail("four-2D transpose_AB source missing transposed B load")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "four-2D transpose_AB simdgroup metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "four-2D transpose_AB simdgroup metallib bridge did not produce a "
            "library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "four-2D transpose_AB simdgroup launch plan did not carry a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_metallib_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_transpose_ab_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_four_2d_transpose_ab_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_edge_tail_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup metallib package did not "
            f"produce metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup metallib package did not "
            "retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup metallib package did not "
            "use simdgroup source"
        )
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing simdgroup index"
        )
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing simdgroup lane"
        )
    if "threadgroup half A_tile[256];" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing A staging tile"
        )
    if "threadgroup half B_tile[256];" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing B staging tile"
        )
    if "threadgroup float C_tile[256];" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing C staging tile"
        )
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing M tile split"
        )
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing N tile split"
        )
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing tile offset"
        )
    if (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < 9u) ? A[(global_k * 15u) + global_m] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing guarded "
            "transposed A load"
        )
    if (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < 9u && "
        "global_n < 15u) ? B[(global_n * 9u) + global_k] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing guarded "
            "transposed B load"
        )
    if (
        "simdgroup_store(C_frag[0], C_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing staged store"
        )
    if "if (row < 15u && col < 15u) {" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing writeback guard"
        )
    if (
        "C[(row * 15u) + col] = C_tile[simdgroup_tile_offset + c_linear];"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup source missing guarded writeback"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup metallib bridge did not "
            "produce a library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "four-2D transpose_AB edge/tail simdgroup launch plan did not carry "
            "a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_edge_tail_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_four_2d_splitk_atomic_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_splitk_atomic_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_splitk_atomic_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_splitk_atomic_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "four-2D split-K atomic simdgroup metallib package did not produce "
            f"metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "four-2D split-K atomic simdgroup metallib package did not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "four-2D split-K atomic simdgroup metallib package did not use "
            "simdgroup source"
        )
    if "device atomic_float* C [[buffer(2)]]" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing atomic C pointer")
    if "uint3 tgid [[threadgroup_position_in_grid]]" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing 3D threadgroup id")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing simdgroup index")
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing simdgroup lane")
    if "threadgroup float C_tile[256];" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing C staging tile")
    if "uint split_k_index = tgid.z;" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing z-axis split index")
    if "uint split_k0 = split_k_index * 8u;" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing split start")
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing N tile split")
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing tile offset")
    if (
        "simdgroup_load(A_frag[0], A + (((tgid.y * 16u) + "
        "(simdgroup_tile_m * 8u)) * 16u) + (split_k0 + (ko * 8u)), "
        "16u, 0, false);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic source missing split A load")
    if (
        "simdgroup_load(B_frag[0], B + ((split_k0 + (ko * 8u)) * 16u) + "
        "((tgid.x * 16u) + (simdgroup_tile_n * 8u)), 16u, 0, false);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic source missing split B load")
    if (
        "simdgroup_store(C_frag[0], C_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic source missing staged store")
    if "for (uint c_linear = simdgroup_lane; c_linear < 64u; c_linear += 32u) {" not in metal_source:
        pytest.fail("four-2D split-K atomic source missing lane writeback loop")
    if (
        "atomic_fetch_add_explicit(&C[(row * 16u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic source missing atomic accumulation")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "four-2D split-K atomic simdgroup metallib bridge was not "
            f"load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "four-2D split-K atomic simdgroup metallib bridge did not produce "
            "a library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "four-2D split-K atomic simdgroup launch plan did not carry a "
            "metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_four_2d_splitk_atomic_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_splitk_atomic_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_four_2d_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_four_2d_splitk_atomic_edge_tail_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "four-2D split-K atomic edge/tail simdgroup metallib package did "
            f"not produce metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "four-2D split-K atomic edge/tail simdgroup metallib package did "
            "not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "four-2D split-K atomic edge/tail simdgroup metallib package did "
            "not use simdgroup source"
        )
    if "device atomic_float* C [[buffer(2)]]" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing atomic C pointer")
    if "uint3 tgid [[threadgroup_position_in_grid]]" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing 3D tgid")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing simdgroup index")
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing simdgroup lane")
    if "threadgroup half A_tile[256];" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing A staging tile")
    if "threadgroup half B_tile[256];" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing B staging tile")
    if "threadgroup float C_tile[256];" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing C staging tile")
    if "uint split_k_index = tgid.z;" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing z split index")
    if "uint split_k0 = split_k_index * 5u;" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing ceildiv split start")
    if "uint split_k_end = min(split_k0 + 5u, 17u);" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing split end clamp")
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing N tile split")
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing tile offset")
    if (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < split_k_end) ? A[(global_m * 17u) + global_k] : half(0.0);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic edge/tail source missing guarded A load")
    if (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 15u) ? B[(global_k * 15u) + global_n] : half(0.0);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic edge/tail source missing guarded B load")
    if (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic edge/tail source missing tiled A load")
    if (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic edge/tail source missing tiled B load")
    if "if (row < 15u && col < 15u) {" not in metal_source:
        pytest.fail("four-2D split-K atomic edge/tail source missing writeback guard")
    if (
        "atomic_fetch_add_explicit(&C[(row * 15u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        not in metal_source
    ):
        pytest.fail("four-2D split-K atomic edge/tail source missing atomic accumulation")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "four-2D split-K atomic edge/tail simdgroup metallib bridge was "
            f"not load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "four-2D split-K atomic edge/tail simdgroup metallib bridge did "
            "not produce a library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "four-2D split-K atomic edge/tail simdgroup launch plan did not "
            "carry a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_four_2d_splitk_atomic_edge_tail_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail simdgroup metallib "
            f"package did not produce metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail simdgroup metallib "
            "package did not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail simdgroup metallib "
            "package did not use simdgroup source"
        )
    if "device atomic_float* C [[buffer(2)]]" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "atomic C pointer"
        )
    if "uint3 tgid [[threadgroup_position_in_grid]]" not in metal_source:
        pytest.fail("four-2D transpose_AB split-K atomic edge/tail source missing 3D tgid")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "simdgroup index"
        )
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "simdgroup lane"
        )
    if "threadgroup half A_tile[256];" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing A "
            "staging tile"
        )
    if "threadgroup half B_tile[256];" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing B "
            "staging tile"
        )
    if "threadgroup float C_tile[256];" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing C "
            "staging tile"
        )
    if "uint split_k_index = tgid.z;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing z "
            "split index"
        )
    if "uint split_k0 = split_k_index * 5u;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "ceildiv split start"
        )
    if "uint split_k_end = min(split_k0 + 5u, 17u);" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "split end clamp"
        )
    if "uint simdgroup_tile_m = simdgroup_idx / 2u;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing M "
            "tile split"
        )
    if "uint simdgroup_tile_n = simdgroup_idx % 2u;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing N "
            "tile split"
        )
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "tile offset"
        )
    if (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < split_k_end) ? A[(global_k * 15u) + global_m] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed A load"
        )
    if (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 15u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed B load"
        )
    if (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "tiled A load"
        )
    if (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "tiled B load"
        )
    if "if (row < 15u && col < 15u) {" not in metal_source:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "writeback guard"
        )
    if (
        "atomic_fetch_add_explicit(&C[(row * 15u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        not in metal_source
    ):
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail source missing "
            "atomic accumulation"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail simdgroup metallib "
            f"bridge was not load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail simdgroup metallib "
            "bridge did not produce a library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "four-2D transpose_AB split-K atomic edge/tail simdgroup launch "
            "plan did not carry a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail simdgroup metallib "
            f"package did not produce metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail simdgroup metallib "
            "package did not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail simdgroup metallib "
            "package did not use simdgroup source"
        )
    if "device atomic_float* C [[buffer(2)]]" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing atomic C pointer")
    if "uint3 tgid [[threadgroup_position_in_grid]]" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing 3D tgid")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing simdgroup index")
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing simdgroup lane")
    if "threadgroup half A_tile[512];" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing A staging tile")
    if "threadgroup half B_tile[512];" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing B staging tile")
    if "threadgroup float C_tile[512];" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing C staging tile")
    if "uint split_k_index = tgid.z;" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing z split index")
    if "uint split_k0 = split_k_index * 5u;" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing ceildiv split start")
    if "uint split_k_end = min(split_k0 + 5u, 17u);" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing split end clamp")
    if "uint simdgroup_tile_m = simdgroup_idx / 4u;" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 4u;" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing N tile split")
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing tile offset")
    if (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < split_k_end) ? A[(global_k * 15u) + global_m] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed A load"
        )
    if (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 31u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed B load"
        )
    if (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing tiled A load")
    if (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing tiled B load")
    if "if (row < 15u && col < 31u) {" not in metal_source:
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing writeback guard")
    if (
        "atomic_fetch_add_explicit(&C[(row * 31u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        not in metal_source
    ):
        pytest.fail("eight-N transpose_AB split-K atomic edge/tail source missing atomic accumulation")
    if not package.bridge_library_load_validated:
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail simdgroup metallib "
            f"bridge was not load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail simdgroup metallib "
            "bridge did not produce a library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "eight-N transpose_AB split-K atomic edge/tail simdgroup launch "
            "plan did not carry a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail simdgroup metallib "
            f"package did not produce metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail simdgroup metallib "
            "package did not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail simdgroup metallib "
            "package did not use simdgroup source"
        )
    if "device atomic_float* C [[buffer(2)]]" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing "
            "atomic C pointer"
        )
    if "uint3 tgid [[threadgroup_position_in_grid]]" not in metal_source:
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing 3D tgid")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing "
            "simdgroup index"
        )
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing "
            "simdgroup lane"
        )
    if "threadgroup half A_tile[1024];" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing A "
            "staging tile"
        )
    if "threadgroup half B_tile[1024];" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing B "
            "staging tile"
        )
    if "threadgroup float C_tile[1024];" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing C "
            "staging tile"
        )
    if "uint split_k_index = tgid.z;" not in metal_source:
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing z split index")
    if "uint split_k0 = split_k_index * 5u;" not in metal_source:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing "
            "ceildiv split start"
        )
    if "uint split_k_end = min(split_k0 + 5u, 17u);" not in metal_source:
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing split end clamp")
    if "uint simdgroup_tile_m = simdgroup_idx / 4u;" not in metal_source:
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 4u;" not in metal_source:
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing N tile split")
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing tile offset")
    if (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 31u && "
        "global_k < split_k_end) ? A[(global_k * 31u) + global_m] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed A load"
        )
    if (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 31u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed B load"
        )
    if (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing tiled A load")
    if (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing tiled B load")
    if "if (row < 31u && col < 31u) {" not in metal_source:
        pytest.fail("sixteen transpose_AB split-K atomic edge/tail source missing writeback guard")
    if (
        "atomic_fetch_add_explicit(&C[(row * 31u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        not in metal_source
    ):
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail source missing "
            "atomic accumulation"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail simdgroup metallib "
            f"bridge was not load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail simdgroup metallib "
            "bridge did not produce a library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "sixteen transpose_AB split-K atomic edge/tail simdgroup launch "
            "plan did not carry a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_metallib_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = (
        _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    )
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_metallib_package",
        compile_metal=True,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        metal_source_emitter=emit_metal_simdgroup_gemm_source,
        metal_source_tool="pcc.kernel_ir.metal_finalize.emit_metal_simdgroup_gemm_source",
        timeout=90.0,
    )
    if package.finalize.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(package.finalize.reason)
    if not package.finalize.metallib_produced:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail simdgroup "
            f"metallib package did not produce metallib: {package.to_dict()}"
        )
    metal_source = package.finalize.metal_source
    if metal_source is None:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail simdgroup "
            "metallib package did not retain source"
        )
    if "simdgroup_multiply_accumulate" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail simdgroup "
            "metallib package did not use simdgroup source"
        )
    if "device atomic_float* C [[buffer(2)]]" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing "
            "atomic C pointer"
        )
    if "uint3 tgid [[threadgroup_position_in_grid]]" not in metal_source:
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing 3D tgid")
    if "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing "
            "simdgroup index"
        )
    if "uint simdgroup_lane [[thread_index_in_simdgroup]]" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing "
            "simdgroup lane"
        )
    if "threadgroup half A_tile[2048];" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing A "
            "staging tile"
        )
    if "threadgroup half B_tile[2048];" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing B "
            "staging tile"
        )
    if "threadgroup float C_tile[2048];" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing C "
            "staging tile"
        )
    if "uint split_k_index = tgid.z;" not in metal_source:
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing z split index")
    if "uint split_k0 = split_k_index * 5u;" not in metal_source:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing "
            "ceildiv split start"
        )
    if "uint split_k_end = min(split_k0 + 5u, 17u);" not in metal_source:
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing split end clamp")
    if "uint simdgroup_tile_m = simdgroup_idx / 8u;" not in metal_source:
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing M tile split")
    if "uint simdgroup_tile_n = simdgroup_idx % 8u;" not in metal_source:
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing N tile split")
    if "uint simdgroup_tile_offset = simdgroup_idx * 64u;" not in metal_source:
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing tile offset")
    if (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 31u && "
        "global_k < split_k_end) ? A[(global_k * 31u) + global_m] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed A load"
        )
    if (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 63u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        not in metal_source
    ):
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing "
            "guarded transposed B load"
        )
    if (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing tiled A load")
    if (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        not in metal_source
    ):
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing tiled B load")
    if "if (row < 31u && col < 63u) {" not in metal_source:
        pytest.fail("thirty-two transpose_AB split-K atomic edge/tail source missing writeback guard")
    if (
        "atomic_fetch_add_explicit(&C[(row * 63u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        not in metal_source
    ):
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail source missing "
            "atomic accumulation"
        )
    if not package.bridge_library_load_validated:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail simdgroup "
            f"metallib bridge was not load-validated: {package.to_dict()}"
        )
    if package.bridge_library_path is None:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail simdgroup "
            "metallib bridge did not produce a library path"
        )
    if package.launch_plan.metallib_path is None:
        pytest.fail(
            "thirty-two transpose_AB split-K atomic edge/tail simdgroup launch "
            "plan did not carry a metallib path"
        )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_metallib_native_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    a, b = (
        _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    )
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_two_n_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_two_n_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_two_n_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_two_n_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 2u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 2u;" in metal_source
    assert (
        "simdgroup_store(C_frag[0], C + ((tgid.y * 8u) * 16u) + "
        "((tgid.x * 16u) + (simdgroup_tile_n * 8u)), 16u, 0, false);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_two_n_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_two_n_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_two_n_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_two_m_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_two_m_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_two_m_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_two_m_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 1u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 1u;" in metal_source
    assert (
        "simdgroup_store(C_frag[0], C + (((tgid.y * 16u) + "
        "(simdgroup_tile_m * 8u)) * 8u) + (tgid.x * 8u), 8u, 0, false);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_two_m_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_two_m_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_two_m_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_four_2d_gemm_artifacts(tmp_path: Path):
    module = _simdgroup_pcc1_four_2d_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 2u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 2u;" in metal_source
    assert (
        "simdgroup_store(C_frag[0], C + (((tgid.y * 16u) + "
        "(simdgroup_tile_m * 8u)) * 16u) + ((tgid.x * 16u) + "
        "(simdgroup_tile_n * 8u)), 16u, 0, false);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_four_2d_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_four_2d_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_four_2d_transpose_ab_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_transpose_ab_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_transpose_ab_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_transpose_ab_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 2u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 2u;" in metal_source
    assert (
        "simdgroup_load(A_frag[0], A + ((ko * 8u) * 16u) + "
        "((tgid.y * 16u) + (simdgroup_tile_m * 8u)), 16u, 0, true);"
        in metal_source
    )
    assert (
        "simdgroup_load(B_frag[0], B + (((tgid.x * 16u) + "
        "(simdgroup_tile_n * 8u)) * 8u) + (ko * 8u), 8u, 0, true);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_four_2d_transpose_ab_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_four_2d_transpose_ab_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_transpose_ab_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_four_2d_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_edge_tail_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_edge_tail_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup half A_tile[256];" in metal_source
    assert "threadgroup half B_tile[256];" in metal_source
    assert "threadgroup float C_tile[256];" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 2u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 2u;" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < 9u) ? A[(global_m * 9u) + global_k] : half(0.0);"
        in metal_source
    )
    assert (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < 9u && "
        "global_n < 15u) ? B[(global_k * 15u) + global_n] : half(0.0);"
        in metal_source
    )
    assert (
        "simdgroup_store(C_frag[0], C_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "if (row < 15u && col < 15u) {" in metal_source
    assert (
        "C[(row * 15u) + col] = C_tile[simdgroup_tile_offset + c_linear];"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_four_2d_edge_tail_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_four_2d_edge_tail_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_four_2d_transpose_ab_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_transpose_ab_edge_tail_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup half A_tile[256];" in metal_source
    assert "threadgroup half B_tile[256];" in metal_source
    assert "threadgroup float C_tile[256];" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 2u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 2u;" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < 9u) ? A[(global_k * 15u) + global_m] : half(0.0);"
        in metal_source
    )
    assert (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < 9u && "
        "global_n < 15u) ? B[(global_n * 9u) + global_k] : half(0.0);"
        in metal_source
    )
    assert (
        "simdgroup_store(C_frag[0], C_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "if (row < 15u && col < 15u) {" in metal_source
    assert (
        "C[(row * 15u) + col] = C_tile[simdgroup_tile_offset + c_linear];"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_edge_tail_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_four_2d_transpose_ab_edge_tail_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_transpose_ab_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_four_2d_splitk_atomic_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_splitk_atomic_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_splitk_atomic_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_splitk_atomic_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint3 tgid [[threadgroup_position_in_grid]]" in metal_source
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup float C_tile[256];" in metal_source
    assert "uint split_k_index = tgid.z;" in metal_source
    assert "uint split_k0 = split_k_index * 8u;" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "simdgroup_load(A_frag[0], A + (((tgid.y * 16u) + "
        "(simdgroup_tile_m * 8u)) * 16u) + (split_k0 + (ko * 8u)), "
        "16u, 0, false);"
        in metal_source
    )
    assert (
        "simdgroup_load(B_frag[0], B + ((split_k0 + (ko * 8u)) * 16u) + "
        "((tgid.x * 16u) + (simdgroup_tile_n * 8u)), 16u, 0, false);"
        in metal_source
    )
    assert (
        "simdgroup_store(C_frag[0], C_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "for (uint c_linear = simdgroup_lane; c_linear < 64u; c_linear += 32u) {" in metal_source
    assert (
        "atomic_fetch_add_explicit(&C[(row * 16u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path / "real_pcc1_simdgroup_four_2d_splitk_atomic_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path / "real_pcc1_simdgroup_four_2d_splitk_atomic_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_splitk_atomic_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_four_2d_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_gemm_args(),
        tmp_path / "real_pcc1_simdgroup_four_2d_splitk_atomic_edge_tail_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint3 tgid [[threadgroup_position_in_grid]]" in metal_source
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup half A_tile[256];" in metal_source
    assert "threadgroup half B_tile[256];" in metal_source
    assert "threadgroup float C_tile[256];" in metal_source
    assert "uint split_k_index = tgid.z;" in metal_source
    assert "uint split_k0 = split_k_index * 5u;" in metal_source
    assert "uint split_k_end = min(split_k0 + 5u, 17u);" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < split_k_end) ? A[(global_m * 17u) + global_k] : half(0.0);"
        in metal_source
    )
    assert (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 15u) ? B[(global_k * 15u) + global_n] : half(0.0);"
        in metal_source
    )
    assert (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "if (row < 15u && col < 15u) {" in metal_source
    assert (
        "atomic_fetch_add_explicit(&C[(row * 15u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_four_2d_splitk_atomic_edge_tail_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path
        / "real_pcc1_simdgroup_four_2d_splitk_atomic_edge_tail_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint3 tgid [[threadgroup_position_in_grid]]" in metal_source
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup half A_tile[256];" in metal_source
    assert "threadgroup half B_tile[256];" in metal_source
    assert "threadgroup float C_tile[256];" in metal_source
    assert "uint split_k_index = tgid.z;" in metal_source
    assert "uint split_k0 = split_k_index * 5u;" in metal_source
    assert "uint split_k_end = min(split_k0 + 5u, 17u);" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < split_k_end) ? A[(global_k * 15u) + global_m] : half(0.0);"
        in metal_source
    )
    assert (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 15u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        in metal_source
    )
    assert (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "if (row < 15u && col < 15u) {" in metal_source
    assert (
        "atomic_fetch_add_explicit(&C[(row * 15u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path
        / "real_pcc1_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_four_2d_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint3 tgid [[threadgroup_position_in_grid]]" in metal_source
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup half A_tile[512];" in metal_source
    assert "threadgroup half B_tile[512];" in metal_source
    assert "threadgroup float C_tile[512];" in metal_source
    assert "uint split_k_index = tgid.z;" in metal_source
    assert "uint split_k0 = split_k_index * 5u;" in metal_source
    assert "uint split_k_end = min(split_k0 + 5u, 17u);" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 4u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 4u;" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 15u && "
        "global_k < split_k_end) ? A[(global_k * 15u) + global_m] : half(0.0);"
        in metal_source
    )
    assert (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 31u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        in metal_source
    )
    assert (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "if (row < 15u && col < 31u) {" in metal_source
    assert (
        "atomic_fetch_add_explicit(&C[(row * 31u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path
        / "real_pcc1_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_eight_n_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint3 tgid [[threadgroup_position_in_grid]]" in metal_source
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup half A_tile[1024];" in metal_source
    assert "threadgroup half B_tile[1024];" in metal_source
    assert "threadgroup float C_tile[1024];" in metal_source
    assert "uint split_k_index = tgid.z;" in metal_source
    assert "uint split_k0 = split_k_index * 5u;" in metal_source
    assert "uint split_k_end = min(split_k0 + 5u, 17u);" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 4u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 4u;" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 31u && "
        "global_k < split_k_end) ? A[(global_k * 31u) + global_m] : half(0.0);"
        in metal_source
    )
    assert (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 31u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        in metal_source
    )
    assert (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "if (row < 31u && col < 31u) {" in metal_source
    assert (
        "atomic_fetch_add_explicit(&C[(row * 31u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path
        / "real_pcc1_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_sixteen_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def _build_real_runtime_source_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
    tmp_path: Path,
):
    module = (
        _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_module()
    )
    package = build_metal_kernel_package(
        module,
        _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_args(),
        tmp_path
        / "real_pcc1_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_package",
        compile_bridge=False,
    )
    metal_source = emit_metal_simdgroup_gemm_source(module)
    assert "device atomic_float* C [[buffer(2)]]" in metal_source
    assert "uint3 tgid [[threadgroup_position_in_grid]]" in metal_source
    assert "uint simdgroup_idx [[simdgroup_index_in_threadgroup]]" in metal_source
    assert "uint simdgroup_lane [[thread_index_in_simdgroup]]" in metal_source
    assert "threadgroup half A_tile[2048];" in metal_source
    assert "threadgroup half B_tile[2048];" in metal_source
    assert "threadgroup float C_tile[2048];" in metal_source
    assert "uint split_k_index = tgid.z;" in metal_source
    assert "uint split_k0 = split_k_index * 5u;" in metal_source
    assert "uint split_k_end = min(split_k0 + 5u, 17u);" in metal_source
    assert "uint simdgroup_tile_m = simdgroup_idx / 8u;" in metal_source
    assert "uint simdgroup_tile_n = simdgroup_idx % 8u;" in metal_source
    assert "uint simdgroup_tile_offset = simdgroup_idx * 64u;" in metal_source
    assert (
        "A_tile[simdgroup_tile_offset + tile_linear] = (global_m < 31u && "
        "global_k < split_k_end) ? A[(global_k * 31u) + global_m] : half(0.0);"
        in metal_source
    )
    assert (
        "B_tile[simdgroup_tile_offset + tile_linear] = (global_k < split_k_end "
        "&& global_n < 63u) ? B[(global_n * 17u) + global_k] : half(0.0);"
        in metal_source
    )
    assert (
        "simdgroup_load(A_frag[0], A_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert (
        "simdgroup_load(B_frag[0], B_tile + simdgroup_tile_offset, 8u, 0, false);"
        in metal_source
    )
    assert "if (row < 31u && col < 63u) {" in metal_source
    assert (
        "atomic_fetch_add_explicit(&C[(row * 63u) + col], "
        "C_tile[simdgroup_tile_offset + c_linear], memory_order_relaxed);"
        in metal_source
    )

    native_runtime = build_metal_native_buffer_runtime_artifacts(
        tmp_path
        / "real_pcc1_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_native_buffer_runtime",
        compile_runtime=True,
        link_runtime_library=True,
        validate_symbols=True,
    )
    if native_runtime.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(native_runtime.reason)
    if native_runtime.status != STATUS_NATIVE_BUFFER_RUNTIME_LOAD_VALIDATED:
        pytest.fail(
            f"native buffer runtime was not load-validated: {native_runtime.to_dict()}"
        )
    if native_runtime.library_path is None:
        pytest.fail("native buffer runtime did not produce a library path")

    source_bridge = build_metal_source_runtime_bridge_artifacts(
        package.launch_plan,
        tmp_path
        / "real_pcc1_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_source_runtime_bridge",
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        timeout=90.0,
    )
    if source_bridge.status == STATUS_SKIPPED_WITH_REASON:
        pytest.fail(source_bridge.reason)
    if source_bridge.status != STATUS_SOURCE_RUNTIME_BRIDGE_LOAD_VALIDATED:
        pytest.fail(
            f"source runtime bridge was not load-validated: {source_bridge.to_dict()}"
        )
    if source_bridge.library_path is None:
        pytest.fail("source runtime bridge did not produce a library path")

    a, b = _simdgroup_pcc1_thirty_two_transpose_ab_splitk_atomic_edge_tail_input_matrices()
    cpu = execute_scalar_tiled_gemm_reference(module, {"A": a, "B": b})
    return package, native_runtime, source_bridge, metal_source, a, b, cpu.outputs["C"]


def test_level5_pcc1_gate_reports_mode_labeled_skip_when_not_enabled(monkeypatch):
    monkeypatch.delenv("PCC_RUN_GPU_PCC1_LAUNCH", raising=False)
    monkeypatch.delenv("PCC_REQUIRE_CURRENT_PCC1", raising=False)

    gate = _pcc1_launcher_gate_or_reason(REPO)
    assert isinstance(gate, str)
    assert gate.startswith("SKIPPED_WITH_REASON:")

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_scalar_gemm",
        {
            "status": STATUS_SKIPPED_WITH_REASON,
            "reason": gate,
            "whole_program_gpu": False,
        },
    )
    checked = require_pcc1_native_or_skip(evidence)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_0_METADATA
    assert checked.proven is False
    assert checked.pcc1_native_executed is False


def test_level5_classifier_does_not_upgrade_level4_device_result_without_pcc1():
    evidence = classify_pcc1_native_gpu_result(
        "tilelang_scalar_gemm",
        _level4_package_dict(),
    )

    assert evidence.level is GpuClaimLevel.GPU_LEVEL_4_DEVICE_RESULT
    assert evidence.device_result_proven is True
    assert evidence.pcc1_native_executed is False
    with pytest.raises(GpuClaimError, match="GPU_LEVEL_5_PCC1_NATIVE"):
        require_pcc1_native_or_skip(evidence)


def test_level5_classifier_requires_no_libpython_and_same_launcher_path():
    result = {
        **_level4_package_dict(),
        "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
        "pcc1_native_executed": True,
        "pcc1_no_libpython": True,
        "same_launcher_path": False,
        "pcc1_returncode": 0,
    }
    evidence = classify_pcc1_native_gpu_result("tilelang_scalar_gemm", result)

    assert evidence.level is GpuClaimLevel.GPU_LEVEL_4_DEVICE_RESULT
    with pytest.raises(GpuClaimError, match="GPU_LEVEL_5_PCC1_NATIVE"):
        require_pcc1_native_or_skip(evidence)

    result["same_launcher_path"] = True
    evidence = classify_pcc1_native_gpu_result("tilelang_scalar_gemm", result)
    checked = require_pcc1_native_or_skip(evidence)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.pcc1_native_executed is True


def test_level5_classifier_accepts_pcc1_metallib_device_result():
    result = {
        "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
        "runtime_launch_executed": True,
        "runtime_source_compiled": False,
        "whole_program_gpu": False,
        "finalize": {"metallib_produced": True},
        "invocation": {
            "status": STATUS_BRIDGE_INVOKED,
            "fence_completed": True,
        },
        "cpu_comparison": {"status": "metal_cpu_oracle_match"},
        "pcc1_native_executed": True,
        "pcc1_no_libpython": True,
        "same_launcher_path": True,
        "pcc1_returncode": 0,
    }
    evidence = classify_pcc1_native_gpu_result("metallib_copy", result)
    checked = require_pcc1_native_or_skip(evidence, strict=True)

    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.metallib_produced is True
    assert checked.runtime_source_compiled is False
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_preflight_reports_current_launcher_blockers():
    preflight = analyze_pcc1_metal_launcher_preflight(REPO)

    assert preflight.status == STATUS_PCC1_METAL_PREFLIGHT_BLOCKED
    assert preflight.blocked is True
    assert "pcc.kernel_ir.metal_source_runtime" in preflight.visited_modules

    blocker_codes = {blocker.code for blocker in preflight.blockers}
    blocker_modules = {blocker.module for blocker in preflight.blockers}
    assert "ctypes_dynamic_ffi" in blocker_codes
    assert "ctypes_cdll_load" in blocker_codes
    assert "ctypes_callback" not in blocker_codes
    assert "host_subprocess_toolchain" in blocker_codes
    assert "pcc.kernel_ir.metal_source_runtime" in blocker_modules
    assert "pcc.kernel_ir.metal_buffer" in blocker_modules
    assert "pcc.backend.metal" in blocker_modules

    runtime_codes = {blocker.code for blocker in preflight.runtime_blockers}
    build_codes = {blocker.code for blocker in preflight.build_blockers}
    assert "ctypes_dynamic_ffi" in runtime_codes
    assert "ctypes_cdll_load" in runtime_codes
    assert "host_subprocess_toolchain" not in runtime_codes
    assert "host_subprocess_toolchain" in build_codes


def test_level5_pcc1_runtime_abi_surface_has_no_static_ctypes_blockers():
    preflight = analyze_pcc1_metal_launcher_preflight(
        REPO,
        entry_modules=PCC1_METAL_RUNTIME_ABI_ENTRY_MODULES,
    )

    assert preflight.status == STATUS_PCC1_METAL_PREFLIGHT_READY
    assert preflight.blocked is False
    assert preflight.runtime_blockers == ()
    assert "pcc.kernel_ir.metal_runtime_abi" in preflight.visited_modules


def test_level5_pcc1_compiled_program_calls_runtime_c_shim_fake_bridge(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 C-shim gate; "
                "set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail("no fresh current pcc1 binary for GPU Level-5 C-shim gate")
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    bridge = _build_fake_runtime_source_bridge(tmp_path)
    source_literal = "kernel void k(){}"
    src = tmp_path / "pcc1_metal_runtime_probe.py"
    exe = tmp_path / "pcc1_metal_runtime_probe"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i8,
                load_i64,
                malloc,
                store_f64,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_length = extern(
                "pcc_metal_buffer_runtime_length_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                runtime = cstr({json.dumps(str(bridge))})
                out_buffer = malloc(8)
                out_nbytes = malloc(8)
                buffers = malloc(8)
                payload = malloc(9)
                readback = malloc(9)
                scalar_payload = malloc(17)
                scalar_offsets = malloc(24)

                rc = pcc_buffer_create(runtime, 64, out_buffer)
                if rc != 0:
                    print(rc)
                    return
                buffer_ptr: int = load_i64(out_buffer, 0)
                store_i64(buffers, 0, buffer_ptr)
                rc = pcc_buffer_length(runtime, buffer_ptr, out_nbytes)
                if rc != 0:
                    print(rc)
                    return
                if load_i64(out_nbytes, 0) != 128:
                    print(900)
                    return

                store_i8(payload, 0, 112)
                store_i8(payload, 1, 99)
                store_i8(payload, 2, 99)
                store_i8(payload, 3, 45)
                store_i8(payload, 4, 109)
                store_i8(payload, 5, 101)
                store_i8(payload, 6, 116)
                store_i8(payload, 7, 97)
                store_i8(payload, 8, 108)
                rc = pcc_buffer_write(runtime, buffer_ptr, 4, payload, 9)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_read(runtime, buffer_ptr, 4, readback, 9)
                if rc != 0:
                    print(rc)
                    return
                if load_i8(readback, 0) != 112 or load_i8(readback, 8) != 108:
                    print(901)
                    return

                store_i32(scalar_payload, 0, 7)
                store_f64(scalar_payload, 8, 1.5)
                store_i8(scalar_payload, 16, 1)
                store_i64(scalar_offsets, 0, 0)
                store_i64(scalar_offsets, 8, 8)
                store_i64(scalar_offsets, 16, 16)

                rc = pcc_metal_call(
                    runtime,
                    cstr("pcc_fake_source_bridge"),
                    cstr({json.dumps(source_literal)}),
                    {len(source_literal)},
                    buffers,
                    1,
                    scalar_payload,
                    scalar_offsets,
                    3,
                    1,
                )
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(runtime, buffer_ptr)
                print(rc)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(payload)
                free(buffers)
                free(out_nbytes)
                free(out_buffer)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "0"


def test_level5_pcc1_compiled_program_runs_real_runtime_source_copy(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 real runtime-source gate; "
                "set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail("no fresh current pcc1 binary for GPU Level-5 real runtime-source gate")
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source = (
        _build_real_runtime_source_copy_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    requested_owner = os.environ.get(
        "PCC_TEST_LEVEL5_COPY_GPU_OWNER",
        GPU_BACKEND_PCC_METAL,
    )
    provider_result = None
    if requested_owner == GPU_BACKEND_TVM_TILELANG:
        provider_result = compile_with_tvm_tilelang_provider(
            lower_to_plain_tir(_copy_module(), target="metal"),
            tmp_path / "tvm_tilelang_provider",
            pipeline=TVM_TILELANG_PIPELINE,
        )
        metal_source = provider_result.metal_source
    elif requested_owner != GPU_BACKEND_PCC_METAL:
        pytest.fail(f"unsupported explicit Level-5 GPU owner {requested_owner!r}")

    src = tmp_path / "pcc1_real_metal_runtime_source_copy.py"
    exe = tmp_path / "pcc1_real_metal_runtime_source_copy"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                getenv,
                load_i8,
                load_i32,
                load_i64,
                malloc,
                ptr_is_null,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_src = malloc(8)
                out_dst = malloc(8)
                buffers = malloc(16)
                scalar_payload = malloc(4)
                scalar_offsets = malloc(8)
                src_payload = malloc(16)
                readback = malloc(16)

                rc = pcc_buffer_create(buffer_runtime, 16, out_src)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 16, out_dst)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                src_ptr: int = load_i64(out_src, 0)
                dst_ptr: int = load_i64(out_dst, 0)
                store_i64(buffers, 0, src_ptr)
                store_i64(buffers, 8, dst_ptr)

                store_i32(src_payload, 0, 1065353216)
                store_i32(src_payload, 4, 1073741824)
                store_i32(src_payload, 8, 1080033280)
                store_i32(src_payload, 12, 1082654720)
                rc = pcc_buffer_write(buffer_runtime, src_ptr, 0, src_payload, 16)
                if rc != 0:
                    print(rc)
                    return

                store_i32(scalar_payload, 0, 4)
                store_i64(scalar_offsets, 0, 0)
                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    2,
                    scalar_payload,
                    scalar_offsets,
                    1,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, dst_ptr, 0, readback, 16)
                if rc != 0:
                    print(rc)
                    return
                if load_i32(readback, 0) != 1065353216:
                    print(910)
                    return
                if load_i32(readback, 4) != 1073741824:
                    print(911)
                    return
                if load_i32(readback, 8) != 1080033280:
                    print(912)
                    return
                if load_i32(readback, 12) != 1082654720:
                    print(913)
                    return

                rc = pcc_buffer_release(buffer_runtime, dst_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, src_ptr)
                if rc != 0:
                    print(rc)
                    return
                gc_backend = getenv(cstr("PCC_GC_BACKEND"))
                if not ptr_is_null(gc_backend):
                    print(load_i8(gc_backend, 0))
                print(0)

                free(readback)
                free(src_payload)
                free(scalar_offsets)
                free(scalar_payload)
                free(buffers)
                free(out_dst)
                free(out_src)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_GC_BACKEND", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 real Metal runtime-source probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    if provider_result is None:
        owner_identity = pcc_metal_owner_identity_fields(
            launcher_links_libpython=False
        )
        provider_dependencies = None
    else:
        owner_identity = tvm_tilelang_owner_identity_fields(
            launcher_links_libpython=False,
            provider_identity=provider_result.provider_identity,
            pipeline=provider_result.pipeline,
        )
        provider_dependencies = dict(provider_result.dependencies)
    owner_result = {
        **owner_identity,
        "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
        "runtime_launch_executed": True,
        "runtime_source_compiled": True,
        "whole_program_gpu": False,
        "finalize": {"metallib_produced": False},
        "invocation": {
            "status": STATUS_SOURCE_RUNTIME_INVOKED,
            "fence_completed": True,
        },
        "cpu_comparison": {"status": "metal_cpu_oracle_match"},
        "pcc1_native_executed": True,
        "pcc1_no_libpython": True,
        "same_launcher_path": True,
        "pcc1_returncode": run.returncode,
        "provider_dependencies": provider_dependencies,
    }
    validate_gpu_owner_identity(
        owner_result,
        requested_gpu_backend=requested_owner,
    )
    evidence = classify_pcc1_native_gpu_result(
        "runtime_source_copy",
        owner_result,
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tvm_tilelang_owner_copy(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "PCC_TEST_LEVEL5_COPY_GPU_OWNER",
        GPU_BACKEND_TVM_TILELANG,
    )
    test_level5_pcc1_compiled_program_runs_real_runtime_source_copy(tmp_path)


def test_level5_pcc1_compiled_program_runs_real_metallib_copy(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 real metallib gate; "
                "set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail("no fresh current pcc1 binary for GPU Level-5 real metallib gate")
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime = _build_real_metallib_copy_artifacts(tmp_path)
    assert package.finalize.metallib_produced is True
    assert package.runtime_launch_executed is False

    src = tmp_path / "pcc1_real_metal_metallib_copy.py"
    exe = tmp_path / "pcc1_real_metal_metallib_copy"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_metallib_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                bridge_runtime = cstr({json.dumps(str(package.bridge_library_path))})
                symbol = cstr({json.dumps(str(package.bridge_library_symbol))})
                metallib_path = cstr({json.dumps(str(package.launch_plan.metallib_path))})
                out_src = malloc(8)
                out_dst = malloc(8)
                buffers = malloc(16)
                scalar_payload = malloc(4)
                scalar_offsets = malloc(8)
                src_payload = malloc(16)
                readback = malloc(16)

                rc = pcc_buffer_create(buffer_runtime, 16, out_src)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 16, out_dst)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                src_ptr: int = load_i64(out_src, 0)
                dst_ptr: int = load_i64(out_dst, 0)
                store_i64(buffers, 0, src_ptr)
                store_i64(buffers, 8, dst_ptr)

                store_i32(src_payload, 0, 1065353216)
                store_i32(src_payload, 4, 1073741824)
                store_i32(src_payload, 8, 1080033280)
                store_i32(src_payload, 12, 1082654720)
                rc = pcc_buffer_write(buffer_runtime, src_ptr, 0, src_payload, 16)
                if rc != 0:
                    print(rc)
                    return

                store_i32(scalar_payload, 0, 4)
                store_i64(scalar_offsets, 0, 0)
                rc = pcc_metal_call(
                    bridge_runtime,
                    symbol,
                    metallib_path,
                    buffers,
                    2,
                    scalar_payload,
                    scalar_offsets,
                    1,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, dst_ptr, 0, readback, 16)
                if rc != 0:
                    print(rc)
                    return
                if load_i32(readback, 0) != 1065353216:
                    print(910)
                    return
                if load_i32(readback, 4) != 1073741824:
                    print(911)
                    return
                if load_i32(readback, 8) != 1080033280:
                    print(912)
                    return
                if load_i32(readback, 12) != 1082654720:
                    print(913)
                    return

                rc = pcc_buffer_release(buffer_runtime, dst_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, src_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(readback)
                free(src_payload)
                free(scalar_offsets)
                free(scalar_payload)
                free(buffers)
                free(out_dst)
                free(out_src)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 real Metal metallib probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "metallib_copy",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": False,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": True},
            "invocation": {
                "status": STATUS_BRIDGE_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is False
    assert checked.metallib_produced is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_gemm_metallib(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang GEMM "
                "metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang GEMM metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime = _build_real_metallib_tilelang_gemm_artifacts(tmp_path)
    assert package.finalize.metallib_produced is True
    assert package.runtime_launch_executed is False

    module = _tilelang_gemm_module()
    a = ((1.0, 2.0), (3.0, 4.0))
    b = ((5.0, 6.0), (7.0, 8.0))
    expected = execute_scalar_tiled_gemm_reference(
        module,
        {"A": a, "B": b},
    ).outputs["C"]
    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=940,
    )

    src = tmp_path / "pcc1_real_tilelang_gemm_metallib.py"
    exe = tmp_path / "pcc1_real_tilelang_gemm_metallib"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_metallib_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                bridge_runtime = cstr({json.dumps(str(package.bridge_library_path))})
                symbol = cstr({json.dumps(str(package.bridge_library_symbol))})
                metallib_path = cstr({json.dumps(str(package.launch_plan.metallib_path))})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(8)
                b_payload = malloc(8)
                readback = malloc(16)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 8, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 8, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 16, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 8)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 8)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    bridge_runtime,
                    symbol,
                    metallib_path,
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 16)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 TileLang GEMM metallib probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_scalar_gemm_metallib",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": False,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": True},
            "invocation": {
                "status": STATUS_BRIDGE_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is False
    assert checked.metallib_produced is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_local_tilelang_metal_benchmark_metallib(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 local TileLang "
                "Metal benchmark metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 local TileLang "
            "Metal benchmark metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_tilelang_local_metal_benchmark_artifacts(tmp_path)
    )
    _run_pcc1_three_buffer_metallib_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 3 * 2,
        b_nbytes=3 * 7 * 2,
        c_nbytes=5 * 7 * 4,
        first_error_code=20900,
        src_stem="pcc1_real_local_tilelang_metal_benchmark_metallib",
        failure_label="local TileLang Metal benchmark metallib",
        workload_name="tilelang_local_metal_benchmark_metallib",
    )


def test_level5_pcc1_compiled_program_runs_local_tilelang_matmul_nonroller_metallib(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 local TileLang "
                "matmul non-roller metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 local TileLang "
            "matmul non-roller metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_tilelang_local_matmul_nonroller_artifacts(tmp_path)
    )
    _run_pcc1_three_buffer_metallib_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 3 * 2,
        b_nbytes=7 * 3 * 2,
        c_nbytes=5 * 7 * 2,
        c_dtype="f16",
        first_error_code=21000,
        src_stem="pcc1_real_local_tilelang_matmul_nonroller_metallib",
        failure_label="local TileLang matmul non-roller metallib",
        workload_name="tilelang_local_matmul_nonroller_metallib",
    )


def test_level5_pcc1_compiled_program_runs_local_tilelang_matmul_static_roller_metallib(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 local TileLang "
                "matmul static roller-config metallib gate; set PCC_CURRENT_PCC1 "
                "or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 local TileLang "
            "matmul static roller-config metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_tilelang_local_matmul_static_roller_artifacts(tmp_path)
    )
    _run_pcc1_three_buffer_metallib_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 3 * 2,
        b_nbytes=7 * 3 * 2,
        c_nbytes=5 * 7 * 2,
        c_dtype="f16",
        first_error_code=21100,
        src_stem="pcc1_real_local_tilelang_matmul_static_roller_metallib",
        failure_label="local TileLang matmul static roller-config metallib",
        workload_name="tilelang_local_matmul_static_roller_metallib",
    )


def test_level5_pcc1_compiled_program_runs_imported_tilelang_gemm(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang GEMM gate; "
                "set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail("no fresh current pcc1 binary for GPU Level-5 TileLang GEMM gate")
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source = (
        _build_real_runtime_source_tilelang_gemm_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    src = tmp_path / "pcc1_real_tilelang_gemm.py"
    exe = tmp_path / "pcc1_real_tilelang_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(8)
                b_payload = malloc(8)
                readback = malloc(16)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 8, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 8, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 16, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

                # A = [[1, 2], [3, 4]] as little-endian IEEE-754 half.
                store_i8(a_payload, 0, 0)
                store_i8(a_payload, 1, 60)
                store_i8(a_payload, 2, 0)
                store_i8(a_payload, 3, 64)
                store_i8(a_payload, 4, 0)
                store_i8(a_payload, 5, 66)
                store_i8(a_payload, 6, 0)
                store_i8(a_payload, 7, 68)

                # B = [[5, 6], [7, 8]] as little-endian IEEE-754 half.
                store_i8(b_payload, 0, 0)
                store_i8(b_payload, 1, 69)
                store_i8(b_payload, 2, 0)
                store_i8(b_payload, 3, 70)
                store_i8(b_payload, 4, 0)
                store_i8(b_payload, 5, 71)
                store_i8(b_payload, 6, 0)
                store_i8(b_payload, 7, 72)

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 8)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 8)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 16)
                if rc != 0:
                    print(rc)
                    return
                if load_i32(readback, 0) != 1100480512:
                    print(920)
                    return
                if load_i32(readback, 4) != 1102053376:
                    print(921)
                    return
                if load_i32(readback, 8) != 1110179840:
                    print(922)
                    return
                if load_i32(readback, 12) != 1112014848:
                    print(923)
                    return

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 TileLang GEMM runtime-source probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    owner_result = {
        **pcc_metal_owner_identity_fields(launcher_links_libpython=False),
        "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
        "runtime_launch_executed": True,
        "runtime_source_compiled": True,
        "whole_program_gpu": False,
        "finalize": {"metallib_produced": False},
        "invocation": {
            "status": STATUS_SOURCE_RUNTIME_INVOKED,
            "fence_completed": True,
        },
        "cpu_comparison": {"status": "metal_cpu_oracle_match"},
        "pcc1_native_executed": True,
        "pcc1_no_libpython": True,
        "same_launcher_path": True,
        "pcc1_returncode": run.returncode,
    }
    validate_gpu_owner_identity(
        owner_result,
        requested_gpu_backend=GPU_BACKEND_PCC_METAL,
    )
    evidence = classify_pcc1_native_gpu_result(
        "tilelang_scalar_gemm",
        owner_result,
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_vectorized_nonzero_serial(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang vectorized "
                "nonzero-serial gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang vectorized "
            "nonzero-serial gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_vectorized_nonzero_serial_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=960,
    )

    src = tmp_path / "pcc1_real_tilelang_vectorized_nonzero_serial.py"
    exe = tmp_path / "pcc1_real_tilelang_vectorized_nonzero_serial"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(240)
                b_payload = malloc(336)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 240, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 336, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 240)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 336)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang vectorized nonzero-serial runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_vectorized_nonzero_serial",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def _run_pcc1_three_buffer_runtime_source_probe(
    pcc1: Path,
    tmp_path: Path,
    *,
    package,
    native_runtime,
    source_bridge,
    metal_source: str,
    a,
    b,
    expected,
    a_nbytes: int,
    b_nbytes: int,
    c_nbytes: int,
    first_error_code: int,
    src_stem: str,
    failure_label: str,
    workload_name: str,
):
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=first_error_code,
    )

    src = tmp_path / f"{src_stem}.py"
    exe = tmp_path / src_stem
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc({a_nbytes})
                b_payload = malloc({b_nbytes})
                readback = malloc({c_nbytes})
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, {a_nbytes}, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, {b_nbytes}, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, {c_nbytes}, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, {a_nbytes})
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, {b_nbytes})
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, {c_nbytes})
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed for {failure_label} (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 {failure_label} probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        workload_name,
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True
    return checked


def _run_pcc1_three_buffer_metallib_probe(
    pcc1: Path,
    tmp_path: Path,
    *,
    package,
    native_runtime,
    a,
    b,
    expected,
    a_nbytes: int,
    b_nbytes: int,
    c_nbytes: int,
    c_dtype: str = "f32",
    c_zero_nbytes: int | None = None,
    first_error_code: int,
    src_stem: str,
    failure_label: str,
    workload_name: str,
):
    assert package.finalize.metallib_produced is True
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = (
        _pcc1_zero_i32_payload_lines(
            "c_payload",
            c_zero_nbytes,
            indent="                ",
        )
        if c_zero_nbytes is not None
        else ""
    )
    c_write_lines = (
        "\n".join(
            [
                f"                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, {c_zero_nbytes})",
                "                if rc != 0:",
                "                    print(rc)",
                "                    return",
            ]
        )
        if c_zero_nbytes is not None
        else ""
    )
    if c_dtype == "f32":
        c_assert_lines = _pcc1_assert_f32_matrix_lines(
            "readback",
            expected,
            indent="                ",
            first_error_code=first_error_code,
        )
    elif c_dtype == "f16":
        c_assert_lines = _pcc1_assert_f16_matrix_byte_lines(
            "readback",
            expected,
            indent="                ",
            first_error_code=first_error_code,
        )
    else:
        raise AssertionError(f"unsupported pcc1 C matrix dtype {c_dtype!r}")

    src = tmp_path / f"{src_stem}.py"
    exe = tmp_path / src_stem
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i8,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_metallib_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                bridge_runtime = cstr({json.dumps(str(package.bridge_library_path))})
                symbol = cstr({json.dumps(str(package.bridge_library_symbol))})
                metallib_path = cstr({json.dumps(str(package.launch_plan.metallib_path))})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc({a_nbytes})
                b_payload = malloc({b_nbytes})
                c_payload = malloc({c_zero_nbytes if c_zero_nbytes is not None else 1})
                readback = malloc({c_nbytes})
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, {a_nbytes}, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, {b_nbytes}, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, {c_nbytes}, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, {a_nbytes})
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, {b_nbytes})
                if rc != 0:
                    print(rc)
                    return
{c_write_lines}

                rc = pcc_metal_call(
                    bridge_runtime,
                    symbol,
                    metallib_path,
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, {c_nbytes})
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed for {failure_label} (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 {failure_label} metallib probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        workload_name,
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": False,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": True},
            "invocation": {
                "status": STATUS_BRIDGE_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is False
    assert checked.metallib_produced is True
    assert checked.cpu_oracle_matched is True
    return checked


def test_level5_pcc1_compiled_program_runs_tilelang_vectorized_annotations(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang vectorized "
                "annotations gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang vectorized "
            "annotations gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_vectorized_annotations_artifacts(
            tmp_path
        )
    )
    _run_pcc1_three_buffer_runtime_source_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        source_bridge=source_bridge,
        metal_source=metal_source,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 16 * 2,
        b_nbytes=16 * 7 * 2,
        c_nbytes=5 * 7 * 4,
        first_error_code=8200,
        src_stem="pcc1_real_tilelang_vectorized_annotations",
        failure_label="TileLang vectorized annotations runtime-source",
        workload_name="tilelang_vectorized_annotations",
    )


def test_level5_pcc1_compiled_program_runs_tilelang_splitk_atomic(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang split-K "
                "atomic gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang split-K atomic gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_splitk_atomic_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        140,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=980,
    )

    src = tmp_path / "pcc1_real_tilelang_splitk_atomic.py"
    exe = tmp_path / "pcc1_real_tilelang_splitk_atomic"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(160)
                b_payload = malloc(224)
                c_payload = malloc(140)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 160, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 224, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 160)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 224)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 140)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang split-K atomic runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_splitk_atomic",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_splitk_atomic_ceildiv_tail(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang split-K "
                "atomic ceildiv-tail gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang split-K atomic "
            "ceildiv-tail gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_splitk_atomic_ceildiv_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        140,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1020,
    )

    src = tmp_path / "pcc1_real_tilelang_splitk_atomic_ceildiv.py"
    exe = tmp_path / "pcc1_real_tilelang_splitk_atomic_ceildiv"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(170)
                b_payload = malloc(238)
                c_payload = malloc(140)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 170, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 238, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 170)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 238)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 140)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang split-K atomic ceildiv-tail runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_splitk_atomic_ceildiv_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_splitk_floor_plus_one_ceildiv_metallib(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang split-K "
                "floor-plus-one ceildiv metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang split-K "
            "floor-plus-one ceildiv metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_tilelang_splitk_floor_plus_one_ceildiv_artifacts(
            tmp_path
        )
    )
    _run_pcc1_three_buffer_metallib_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 17 * 2,
        b_nbytes=17 * 7 * 2,
        c_nbytes=5 * 7 * 4,
        c_zero_nbytes=5 * 7 * 4,
        first_error_code=10600,
        src_stem="pcc1_real_tilelang_splitk_floor_plus_one_ceildiv_metallib",
        failure_label="TileLang split-K floor-plus-one ceildiv metallib",
        workload_name="tilelang_splitk_floor_plus_one_ceildiv_metallib",
    )


def test_level5_pcc1_compiled_program_runs_tilelang_output_staged_gemm_metallib(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang "
                "output-staged GEMM metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang "
            "output-staged GEMM metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_tilelang_output_staging_artifacts(tmp_path)
    )
    _run_pcc1_three_buffer_metallib_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 3 * 2,
        b_nbytes=3 * 7 * 2,
        c_nbytes=5 * 7 * 4,
        first_error_code=10700,
        src_stem="pcc1_real_tilelang_output_staged_gemm_metallib",
        failure_label="TileLang output-staged GEMM metallib",
        workload_name="tilelang_output_staged_gemm_metallib",
    )


def test_level5_pcc1_compiled_program_runs_tilelang_output_staged_f16_transpose_b_gemm_metallib(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang "
                "output-staged f16 transpose_B GEMM metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang "
            "output-staged f16 transpose_B GEMM metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_tilelang_output_staging_f16_transpose_b_artifacts(tmp_path)
    )
    _run_pcc1_three_buffer_metallib_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 3 * 2,
        b_nbytes=7 * 3 * 2,
        c_nbytes=5 * 7 * 2,
        c_dtype="f16",
        first_error_code=20700,
        src_stem="pcc1_real_tilelang_output_staged_f16_transpose_b_gemm_metallib",
        failure_label="TileLang output-staged f16 transpose_B GEMM metallib",
        workload_name="tilelang_output_staged_f16_transpose_b_gemm_metallib",
    )


def test_level5_pcc1_compiled_program_runs_tilelang_gemmwarp_policy_alias_metallib(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang "
                "GemmWarpPolicy alias metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang "
            "GemmWarpPolicy alias metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_tilelang_output_staging_f16_transpose_b_policy_alias_artifacts(
            tmp_path
        )
    )
    _run_pcc1_three_buffer_metallib_probe(
        pcc1,
        tmp_path,
        package=package,
        native_runtime=native_runtime,
        a=a,
        b=b,
        expected=expected,
        a_nbytes=5 * 3 * 2,
        b_nbytes=7 * 3 * 2,
        c_nbytes=5 * 7 * 2,
        c_dtype="f16",
        first_error_code=20800,
        src_stem="pcc1_real_tilelang_gemmwarp_policy_alias_metallib",
        failure_label="TileLang GemmWarpPolicy alias metallib",
        workload_name="tilelang_gemmwarp_policy_alias_metallib",
    )


def test_level5_pcc1_compiled_program_runs_tilelang_transpose_ab(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang "
                "transpose_A+transpose_B gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang "
            "transpose_A+transpose_B gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_transpose_ab_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1060,
    )

    src = tmp_path / "pcc1_real_tilelang_transpose_ab.py"
    exe = tmp_path / "pcc1_real_tilelang_transpose_ab"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(30)
                b_payload = malloc(42)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 30, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 42, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 30)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 42)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang transpose_A+transpose_B runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_transpose_ab",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_swizzled_padded_annotate_layout(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang swizzled "
                "padded annotate_layout gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang swizzled padded "
            "annotate_layout gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_swizzled_padded_annotate_layout_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1100,
    )

    src = tmp_path / "pcc1_real_tilelang_swizzled_padded_annotate_layout.py"
    exe = tmp_path / "pcc1_real_tilelang_swizzled_padded_annotate_layout"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(30)
                b_payload = malloc(42)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 30, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 42, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 30)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 42)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang swizzled padded annotate_layout runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_swizzled_padded_annotate_layout",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_enabled_swizzle(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang enabled "
                "use_swizzle gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang enabled "
            "use_swizzle gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_enabled_swizzle_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1140,
    )

    src = tmp_path / "pcc1_real_tilelang_enabled_swizzle.py"
    exe = tmp_path / "pcc1_real_tilelang_enabled_swizzle"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(544)
                b_payload = malloc(608)
                readback = malloc(1292)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 544, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 608, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 1292, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 544)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 608)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 1292)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang enabled use_swizzle runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_enabled_swizzle",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_nonzero_start_pipelined(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang nonzero-start "
                "Pipelined gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang nonzero-start "
            "Pipelined gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_nonzero_start_pipelined_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1500,
    )

    src = tmp_path / "pcc1_real_tilelang_nonzero_start_pipelined.py"
    exe = tmp_path / "pcc1_real_tilelang_nonzero_start_pipelined"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(240)
                b_payload = malloc(336)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 240, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 336, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 240)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 336)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang nonzero-start Pipelined runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_nonzero_start_pipelined",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_step_serial(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang stepped "
                "T.serial gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang stepped "
            "T.serial gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_step_serial_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=7000,
    )

    src = tmp_path / "pcc1_real_tilelang_step_serial.py"
    exe = tmp_path / "pcc1_real_tilelang_step_serial"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(320)
                b_payload = malloc(448)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 320, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 448, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 320)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 448)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang stepped T.serial runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_step_serial",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_step_pipelined(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang stepped "
                "T.Pipelined gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang stepped "
            "T.Pipelined gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_step_pipelined_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=7100,
    )

    src = tmp_path / "pcc1_real_tilelang_step_pipelined.py"
    exe = tmp_path / "pcc1_real_tilelang_step_pipelined"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(320)
                b_payload = malloc(448)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 320, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 448, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 320)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 448)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang stepped T.Pipelined runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_step_pipelined",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_nonzero_step_pipelined(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang nonzero "
                "stepped T.Pipelined gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang nonzero "
            "stepped T.Pipelined gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_nonzero_step_pipelined_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=7200,
    )

    src = tmp_path / "pcc1_real_tilelang_nonzero_step_pipelined.py"
    exe = tmp_path / "pcc1_real_tilelang_nonzero_step_pipelined"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(400)
                b_payload = malloc(560)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 400, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 560, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 400)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 560)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang nonzero stepped T.Pipelined runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_nonzero_step_pipelined",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_tilelang_nonzero_step_serial(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 TileLang nonzero "
                "stepped T.serial gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 TileLang nonzero "
            "stepped T.serial gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_tilelang_nonzero_step_serial_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=7300,
    )

    src = tmp_path / "pcc1_real_tilelang_nonzero_step_serial.py"
    exe = tmp_path / "pcc1_real_tilelang_nonzero_step_serial"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(400)
                b_payload = malloc(560)
                readback = malloc(140)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 400, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 560, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 140, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 400)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 560)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 140)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 TileLang nonzero stepped T.serial runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "tilelang_nonzero_step_serial",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_gemm(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 simdgroup GEMM gate; "
                "set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail("no fresh current pcc1 binary for GPU Level-5 simdgroup GEMM gate")
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_gemm_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=940,
    )

    src = tmp_path / "pcc1_real_simdgroup_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(128)
                b_payload = malloc(128)
                readback = malloc(256)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 128, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 128, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 256, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 128)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 128)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 256)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 simdgroup GEMM runtime-source probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_8x8",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_gemm_metallib(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 simdgroup GEMM "
                "metallib gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 simdgroup GEMM metallib gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, a, b, expected = (
        _build_real_metallib_simdgroup_gemm_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is True
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1040,
    )

    src = tmp_path / "pcc1_real_simdgroup_gemm_metallib.py"
    exe = tmp_path / "pcc1_real_simdgroup_gemm_metallib"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_metallib_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                bridge_runtime = cstr({json.dumps(str(package.bridge_library_path))})
                symbol = cstr({json.dumps(str(package.bridge_library_symbol))})
                metallib_path = cstr({json.dumps(str(package.launch_plan.metallib_path))})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(128)
                b_payload = malloc(128)
                readback = malloc(256)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 128, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 128, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 256, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 128)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 128)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    bridge_runtime,
                    symbol,
                    metallib_path,
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 256)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"pcc1 simdgroup GEMM metallib probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_8x8_metallib",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": False,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": True},
            "invocation": {
                "status": STATUS_BRIDGE_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is False
    assert checked.metallib_produced is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_two_n_gemm(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 two-N simdgroup "
                "GEMM gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 two-N simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_two_n_gemm_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1320,
    )

    src = tmp_path / "pcc1_real_simdgroup_two_n_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_two_n_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(128)
                b_payload = malloc(256)
                readback = malloc(512)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 128, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 256, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 512, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 128)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 256)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 512)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 two-N simdgroup GEMM runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_two_n",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_two_m_gemm(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 two-M simdgroup "
                "GEMM gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 two-M simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_two_m_gemm_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1460,
    )

    src = tmp_path / "pcc1_real_simdgroup_two_m_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_two_m_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(256)
                b_payload = malloc(128)
                readback = malloc(512)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 256, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 128, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 512, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 256)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 128)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 512)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 two-M simdgroup GEMM runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_two_m",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_four_2d_gemm(tmp_path):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 four-2D simdgroup "
                "GEMM gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 four-2D simdgroup "
            "GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_four_2d_gemm_artifacts(tmp_path)
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1600,
    )

    src = tmp_path / "pcc1_real_simdgroup_four_2d_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_four_2d_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(256)
                b_payload = malloc(256)
                readback = malloc(1024)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 256, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 256, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 1024, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 256)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 256)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 1024)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 four-2D simdgroup GEMM runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_four_2d",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_four_2d_transpose_ab_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 four-2D transpose "
                "simdgroup GEMM gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 four-2D transpose "
            "simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_four_2d_transpose_ab_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines("a_payload", a, indent="                ")
    b_store_lines = _pcc1_store_f16_matrix_lines("b_payload", b, indent="                ")
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=1900,
    )

    src = tmp_path / "pcc1_real_simdgroup_four_2d_transpose_ab_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_four_2d_transpose_ab_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(256)
                b_payload = malloc(256)
                readback = malloc(1024)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 256, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 256, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 1024, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 256)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 256)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 1024)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 four-2D transpose simdgroup GEMM runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_four_2d_transpose_ab",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_four_2d_edge_tail_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 four-2D edge-tail "
                "simdgroup GEMM gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 four-2D edge-tail "
            "simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_four_2d_edge_tail_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=2200,
    )

    src = tmp_path / "pcc1_real_simdgroup_four_2d_edge_tail_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_four_2d_edge_tail_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(270)
                b_payload = malloc(270)
                readback = malloc(900)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 270, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 270, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 900, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 270)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 270)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 900)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 four-2D edge-tail simdgroup GEMM runtime-source probe failed "
        f"(exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_four_2d_edge_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_four_2d_transpose_ab_edge_tail_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 four-2D "
                "transpose edge-tail simdgroup GEMM gate; set PCC_CURRENT_PCC1 "
                "or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 four-2D transpose "
            "edge-tail simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_four_2d_transpose_ab_edge_tail_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=2500,
    )

    src = tmp_path / "pcc1_real_simdgroup_four_2d_transpose_ab_edge_tail_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_four_2d_transpose_ab_edge_tail_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(270)
                b_payload = malloc(270)
                readback = malloc(900)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 270, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 270, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 900, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 270)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 270)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 900)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 four-2D transpose edge-tail simdgroup GEMM runtime-source probe "
        f"failed (exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_four_2d_transpose_ab_edge_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_four_2d_splitk_atomic_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 four-2D split-K "
                "atomic simdgroup GEMM gate; set PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 four-2D split-K "
            "atomic simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_four_2d_splitk_atomic_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        1024,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=2800,
    )

    src = tmp_path / "pcc1_real_simdgroup_four_2d_splitk_atomic_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_four_2d_splitk_atomic_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(512)
                b_payload = malloc(512)
                c_payload = malloc(1024)
                readback = malloc(1024)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 512, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 512, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 1024, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 512)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 512)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 1024)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 1024)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 four-2D split-K atomic simdgroup GEMM runtime-source probe "
        f"failed (exit {run.returncode})\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_four_2d_splitk_atomic",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_four_2d_splitk_atomic_edge_tail_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 four-2D split-K "
                "atomic edge-tail simdgroup GEMM gate; set PCC_CURRENT_PCC1 or "
                "rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 four-2D split-K "
            "atomic edge-tail simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_four_2d_splitk_atomic_edge_tail_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        900,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=3400,
    )

    src = tmp_path / "pcc1_real_simdgroup_four_2d_splitk_atomic_edge_tail_gemm.py"
    exe = tmp_path / "pcc1_real_simdgroup_four_2d_splitk_atomic_edge_tail_gemm"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(510)
                b_payload = malloc(510)
                c_payload = malloc(900)
                readback = malloc(900)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 510, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 510, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 900, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 510)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 510)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 900)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 900)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 four-2D split-K atomic edge-tail simdgroup GEMM runtime-source "
        f"probe failed (exit {run.returncode})\nstdout:\n{run.stdout}\n"
        f"stderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_four_2d_splitk_atomic_edge_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 four-2D "
                "transpose split-K atomic edge-tail simdgroup GEMM gate; set "
                "PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 four-2D transpose "
            "split-K atomic edge-tail simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        900,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=4000,
    )

    src = (
        tmp_path
        / "pcc1_real_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm.py"
    )
    exe = (
        tmp_path
        / "pcc1_real_simdgroup_four_2d_transpose_ab_splitk_atomic_edge_tail_gemm"
    )
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(510)
                b_payload = malloc(510)
                c_payload = malloc(900)
                readback = malloc(900)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 510, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 510, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 900, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 510)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 510)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 900)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 900)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 four-2D transpose split-K atomic edge-tail simdgroup GEMM "
        f"runtime-source probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_four_2d_transpose_ab_splitk_atomic_edge_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 eight-simdgroup "
                "transpose split-K atomic edge-tail simdgroup GEMM gate; set "
                "PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 eight-simdgroup "
            "transpose split-K atomic edge-tail simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        1860,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=4500,
    )

    src = (
        tmp_path
        / "pcc1_real_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm.py"
    )
    exe = (
        tmp_path
        / "pcc1_real_simdgroup_eight_n_transpose_ab_splitk_atomic_edge_tail_gemm"
    )
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(510)
                b_payload = malloc(1054)
                c_payload = malloc(1860)
                readback = malloc(1860)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 510, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 1054, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 1860, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 510)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 1054)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 1860)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 1860)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 eight-simdgroup transpose split-K atomic edge-tail simdgroup "
        f"GEMM runtime-source probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_eight_n_transpose_ab_splitk_atomic_edge_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 sixteen-simdgroup "
                "transpose split-K atomic edge-tail simdgroup GEMM gate; set "
                "PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 sixteen-simdgroup "
            "transpose split-K atomic edge-tail simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        3844,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=5200,
    )

    src = (
        tmp_path
        / "pcc1_real_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm.py"
    )
    exe = (
        tmp_path
        / "pcc1_real_simdgroup_sixteen_transpose_ab_splitk_atomic_edge_tail_gemm"
    )
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(1054)
                b_payload = malloc(1054)
                c_payload = malloc(3844)
                readback = malloc(3844)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 1054, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 1054, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 3844, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 1054)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 1054)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 3844)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 3844)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 sixteen-simdgroup transpose split-K atomic edge-tail simdgroup "
        f"GEMM runtime-source probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_sixteen_transpose_ab_splitk_atomic_edge_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_pcc1_compiled_program_runs_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm(
    tmp_path,
):
    pcc1 = _find_current_pcc1(REPO)
    if pcc1 is None:
        if os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1":
            pytest.fail(
                "no fresh current pcc1 binary for GPU Level-5 thirty-two-simdgroup "
                "transpose split-K atomic edge-tail simdgroup GEMM gate; set "
                "PCC_CURRENT_PCC1 or rebuild pcc1"
            )
        pytest.fail(
            "no fresh current pcc1 binary for GPU Level-5 thirty-two-simdgroup "
            "transpose split-K atomic edge-tail simdgroup GEMM gate"
        )
    assert not _links_libpython(pcc1), f"pcc1 links libpython: {pcc1}"

    package, native_runtime, source_bridge, metal_source, a, b, expected = (
        _build_real_runtime_source_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm_artifacts(
            tmp_path
        )
    )
    assert package.finalize.metallib_produced is False
    assert package.runtime_launch_executed is False

    a_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "a_payload",
        a,
        indent="                ",
    )
    b_store_lines = _pcc1_store_f16_matrix_byte_lines(
        "b_payload",
        b,
        indent="                ",
    )
    c_zero_lines = _pcc1_zero_i32_payload_lines(
        "c_payload",
        7812,
        indent="                ",
    )
    c_assert_lines = _pcc1_assert_f32_matrix_lines(
        "readback",
        expected,
        indent="                ",
        first_error_code=6200,
    )

    src = (
        tmp_path
        / "pcc1_real_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm.py"
    )
    exe = (
        tmp_path
        / "pcc1_real_simdgroup_thirty_two_transpose_ab_splitk_atomic_edge_tail_gemm"
    )
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i32,
                load_i64,
                malloc,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                buffer_runtime = cstr({json.dumps(str(native_runtime.library_path))})
                source_runtime = cstr({json.dumps(str(source_bridge.library_path))})
                symbol = cstr({json.dumps(source_bridge.symbol)})
                metal_source = cstr({json.dumps(metal_source)})
                out_a = malloc(8)
                out_b = malloc(8)
                out_c = malloc(8)
                buffers = malloc(24)
                a_payload = malloc(1054)
                b_payload = malloc(2142)
                c_payload = malloc(7812)
                readback = malloc(7812)
                scalar_payload = malloc(1)
                scalar_offsets = malloc(8)

                rc = pcc_buffer_create(buffer_runtime, 1054, out_a)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 2142, out_b)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_create(buffer_runtime, 7812, out_c)
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                a_ptr: int = load_i64(out_a, 0)
                b_ptr: int = load_i64(out_b, 0)
                c_ptr: int = load_i64(out_c, 0)
                store_i64(buffers, 0, a_ptr)
                store_i64(buffers, 8, b_ptr)
                store_i64(buffers, 16, c_ptr)

{a_store_lines}
{b_store_lines}
{c_zero_lines}

                rc = pcc_buffer_write(buffer_runtime, a_ptr, 0, a_payload, 1054)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, b_ptr, 0, b_payload, 2142)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_write(buffer_runtime, c_ptr, 0, c_payload, 7812)
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_metal_call(
                    source_runtime,
                    symbol,
                    metal_source,
                    {len(metal_source.encode("utf-8"))},
                    buffers,
                    3,
                    scalar_payload,
                    scalar_offsets,
                    0,
                    1,
                )
                if rc == 3:
                    print(903)
                    return
                if rc != 0:
                    print(rc)
                    return

                rc = pcc_buffer_read(buffer_runtime, c_ptr, 0, readback, 7812)
                if rc != 0:
                    print(rc)
                    return
{c_assert_lines}

                rc = pcc_buffer_release(buffer_runtime, c_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, b_ptr)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(buffer_runtime, a_ptr)
                if rc != 0:
                    print(rc)
                    return
                print(0)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(c_payload)
                free(b_payload)
                free(a_payload)
                free(buffers)
                free(out_c)
                free(out_b)
                free(out_a)

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            str(src),
            "-o",
            str(exe),
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    assert build.returncode == 0, (
        f"pcc1 compile failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert exe.exists()
    assert not _links_libpython(exe), f"pcc1 output links libpython: {exe}"

    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        "pcc1 thirty-two-simdgroup transpose split-K atomic edge-tail simdgroup "
        f"GEMM runtime-source probe failed (exit {run.returncode})\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    stdout = run.stdout.strip()
    if stdout == "903":
        pytest.fail("MTLCreateSystemDefaultDevice returned nil")
    assert stdout == "0"

    evidence = classify_pcc1_native_gpu_result(
        "simdgroup_gemm_thirty_two_transpose_ab_splitk_atomic_edge_tail",
        {
            "status": STATUS_PCC1_METAL_LAUNCHER_EXECUTED,
            "runtime_launch_executed": True,
            "runtime_source_compiled": True,
            "whole_program_gpu": False,
            "finalize": {"metallib_produced": False},
            "invocation": {
                "status": STATUS_SOURCE_RUNTIME_INVOKED,
                "fence_completed": True,
            },
            "cpu_comparison": {"status": "metal_cpu_oracle_match"},
            "pcc1_native_executed": True,
            "pcc1_no_libpython": True,
            "same_launcher_path": True,
            "pcc1_returncode": run.returncode,
        },
    )
    checked = require_pcc1_native_or_skip(evidence, strict=True)
    assert checked.level is GpuClaimLevel.GPU_LEVEL_5_PCC1_NATIVE
    assert checked.runtime_launch_executed is True
    assert checked.runtime_source_compiled is True
    assert checked.cpu_oracle_matched is True


def test_level5_gate_reports_preflight_blocker_when_run_enabled(
    monkeypatch, tmp_path
):
    fake_pcc1 = tmp_path / "pcc1"
    fake_pcc1.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pcc1.chmod(0o755)
    monkeypatch.setenv("PCC_CURRENT_PCC1", str(fake_pcc1))
    monkeypatch.setenv("PCC_RUN_GPU_PCC1_LAUNCH", "1")
    monkeypatch.delenv("PCC_REQUIRE_CURRENT_PCC1", raising=False)

    gate = _pcc1_launcher_gate_or_reason(REPO)

    assert isinstance(gate, str)
    assert gate.startswith("SKIPPED_WITH_REASON:")
    assert "pcc1 Metal launcher closure is blocked by:" in gate


def test_gpu_level5_pcc1_launcher_real_or_skipped():
    gate = _pcc1_launcher_gate_or_reason(REPO)
    if isinstance(gate, str):
        evidence = classify_pcc1_native_gpu_result(
            "tilelang_scalar_gemm",
            {
                "status": STATUS_SKIPPED_WITH_REASON,
                "reason": gate,
                "whole_program_gpu": False,
            },
        )
        checked = require_pcc1_native_or_skip(evidence)
        assert checked.reason.startswith("SKIPPED_WITH_REASON:")
        return

    pytest.fail(
        "fresh pcc1 is available, but the pcc1-native Metal launcher entrypoint "
        "is not implemented yet; do not upgrade Level-4 host-harness evidence "
        "to GPU_LEVEL_5_PCC1_NATIVE"
    )
