"""Non-executing Metal kernel package manifest for Kernel IR."""

import json

import pytest

from pcc.backend.metal import MetalCompileError, MetalToolchainUnavailable
from pcc.kernel_ir.cpu_reference import KernelCpuReferenceError
from pcc.kernel_ir.hmm_fence import PccBufferHandle, PccPackedArgs
from pcc.kernel_ir.ir import (
    BufferParam,
    KernelFunc,
    KernelModule,
    KernelOp,
    MemoryScope,
    ScalarParam,
    ScalarType,
)
from pcc.kernel_ir.metal_finalize import STATUS_SOURCE_ONLY
from pcc.kernel_ir.metal_launch import (
    STATUS_BRIDGE_OBJECT_PRODUCED,
    STATUS_BRIDGE_SOURCE_ONLY,
)
from pcc.kernel_ir.metal_buffer import (
    MetalNativeBufferBindingError,
    STATUS_NATIVE_BUFFER_BINDINGS_READY,
    build_metal_native_buffer_binding_set,
)
from pcc.kernel_ir.metal_package import (
    MetalPackageError,
    STATUS_BRIDGE_INVOCATION_PACKET_NOT_READY,
    STATUS_BRIDGE_LIBRARY_LOAD_VALIDATED,
    STATUS_BRIDGE_LIBRARY_PRODUCED,
    STATUS_SKIPPED_WITH_REASON,
    build_metal_bridge_invocation_packet,
    build_metal_kernel_package,
    verify_metal_kernel_package_manifest,
    write_metal_kernel_package_manifest,
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


def _module(*, m: int = 5, n: int = 7, k: int = 3):
    return import_tilelang_source(
        TILELANG_METAL_MATMUL,
        outer_function="matmul_simdgroup",
        prim_func="gemm_kernel",
        constants={"M": m, "N": n, "K": k},
        module_name="tilelang_metal_matmul_package",
    )


def _inputs(m: int = 5, n: int = 7, k: int = 3):
    a = [[float(i + kk + 1) for kk in range(k)] for i in range(m)]
    b = [[float((kk * 2) - j) for j in range(n)] for kk in range(k)]
    return a, b


def _packed_args(*, m: int = 5, n: int = 7, k: int = 3) -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=m * k * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=k * n * 2, dtype="f16", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=m * n * 4, dtype="f32", device="metal:0"))
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


def _copy_packed_args() -> PccPackedArgs:
    args = PccPackedArgs(launch_device="metal:0")
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="metal:0"))
    args.add_buffer(PccBufferHandle(nbytes=64, dtype="f32", device="metal:0"))
    args.add_scalar("u32", 16)
    return args


def _fake_bridge_compiler(output_path, *, source_path=None, timeout=30.0):
    output_path.write_bytes(b"fake bridge object")
    return output_path


def _fake_bridge_linker(output_path, *, object_path=None, timeout=30.0):
    output_path.write_bytes(b"fake bridge dylib")
    return output_path


def _fake_bridge_loader(library_path, *, symbol):
    assert library_path.is_file()
    return symbol


def _validated_bridge_package(tmp_path):
    return build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        bridge_compiler=_fake_bridge_compiler,
        bridge_linker=_fake_bridge_linker,
        bridge_loader=_fake_bridge_loader,
    )


def _native_ptrs_for_plan(plan):
    return {
        arg.handle_id: 0x100000 + ordinal * 0x1000
        for ordinal, arg in enumerate(a for a in plan.args if a.kind == "buffer")
    }


def _expected(a, b):
    return tuple(
        tuple(sum(a[i][kk] * b[kk][j] for kk in range(len(a[0]))) for j in range(len(b[0])))
        for i in range(len(a))
    )


def test_metal_kernel_package_manifest_ties_cpu_oracle_source_launch_and_bridge(tmp_path):
    a, b = _inputs()

    result = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        cpu_inputs={"A": a, "B": b},
    )
    data = result.to_dict()

    assert data["status"] == "metal_kernel_package_artifacts"
    assert data["claim_mode"] == "Metal kernel package artifacts, not executed"
    assert data["runtime_launch_executed"] is False
    assert data["whole_program_gpu"] is False
    assert data["cpu_reference"]["outputs"]["C"] == [list(row) for row in _expected(a, b)]
    assert data["finalize"]["status"] == STATUS_SOURCE_ONLY
    assert data["finalize"]["metal_source_produced"] is True
    assert data["finalize"]["metallib_produced"] is False
    assert data["launch_plan"]["runtime_launch_executed"] is False
    assert data["launch_plan"]["metallib_available"] is False
    assert data["bridge"]["status"] == STATUS_BRIDGE_SOURCE_ONLY
    assert data["bridge"]["runtime_launch_executed"] is False
    assert (tmp_path / "tilelang_metal_matmul_package.metal").is_file()
    assert (tmp_path / "gemm_kernel_metal_bridge.m").is_file()


def test_metal_kernel_package_can_optionally_build_bridge_object(tmp_path):
    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        assert source_path is not None
        output_path.write_bytes(b"fake bridge object")
        return output_path

    result = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        bridge_compiler=fake_compiler,
    )

    assert result.bridge.status == STATUS_BRIDGE_OBJECT_PRODUCED
    assert result.bridge.bridge_object_produced is True
    assert result.runtime_launch_executed is False
    assert (tmp_path / "gemm_kernel_metal_bridge.o").read_bytes() == b"fake bridge object"


def test_metal_kernel_package_rejects_cpu_oracle_mismatch_before_artifacts(tmp_path):
    a, b = _inputs()

    with pytest.raises(KernelCpuReferenceError, match="expected 5 rows"):
        build_metal_kernel_package(
            _module(),
            _packed_args(),
            tmp_path,
            cpu_inputs={"A": a[:4], "B": b},
        )

    assert not (tmp_path / "tilelang_metal_matmul_package.metal").exists()
    assert not (tmp_path / "gemm_kernel_metal_bridge.m").exists()


def test_metal_kernel_package_manifest_writes_and_verifies_artifact_hashes(tmp_path):
    a, b = _inputs()
    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        cpu_inputs={"A": a, "B": b},
    )

    manifest_path = write_metal_kernel_package_manifest(package)
    data = verify_metal_kernel_package_manifest(manifest_path)

    assert manifest_path == tmp_path / "metal_kernel_package_manifest.json"
    assert data["manifest_version"] == 1
    assert data["runtime_launch_executed"] is False
    assert data["whole_program_gpu"] is False
    assert set(data["artifacts"]) == {"bridge.source", "finalize.metal_source"}
    for record in data["artifacts"].values():
        assert len(record["sha256"]) == 64
        assert record["nbytes"] > 0


def test_metal_kernel_package_manifest_includes_bridge_object_when_produced(tmp_path):
    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge object")
        return output_path

    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        bridge_compiler=fake_compiler,
    )

    manifest_path = write_metal_kernel_package_manifest(package)
    data = verify_metal_kernel_package_manifest(manifest_path)

    assert set(data["artifacts"]) == {
        "bridge.object",
        "bridge.source",
        "finalize.metal_source",
    }
    assert data["artifacts"]["bridge.object"]["nbytes"] == len(b"fake bridge object")


def test_metal_kernel_package_can_optionally_link_bridge_dylib(tmp_path):
    calls = {}

    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge object")
        return output_path

    def fake_linker(output_path, *, object_path=None, timeout=30.0):
        calls["object_path"] = object_path
        assert object_path is not None
        assert object_path.is_file()
        output_path.write_bytes(b"fake bridge dylib")
        return output_path

    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        bridge_compiler=fake_compiler,
        bridge_linker=fake_linker,
    )
    manifest_path = write_metal_kernel_package_manifest(package)
    data = verify_metal_kernel_package_manifest(manifest_path)

    assert package.bridge_library_status == STATUS_BRIDGE_LIBRARY_PRODUCED
    assert package.bridge_library_produced is True
    assert package.runtime_launch_executed is False
    assert calls["object_path"] == tmp_path / "gemm_kernel_metal_bridge.o"
    assert data["artifacts"]["bridge.library"]["nbytes"] == len(b"fake bridge dylib")
    assert (tmp_path / "gemm_kernel_metal_bridge.dylib").read_bytes() == b"fake bridge dylib"


def test_metal_kernel_package_requires_bridge_object_before_linking(tmp_path):
    with pytest.raises(MetalPackageError, match="requires compile_bridge=True"):
        build_metal_kernel_package(
            _module(),
            _packed_args(),
            tmp_path,
            link_bridge_library=True,
        )


def test_metal_kernel_package_records_linker_unavailable_without_library_artifact(tmp_path):
    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge object")
        return output_path

    def missing_linker(output_path, *, object_path=None, timeout=30.0):
        raise MetalToolchainUnavailable("xcrun clang missing")

    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        bridge_compiler=fake_compiler,
        bridge_linker=missing_linker,
    )
    manifest_path = write_metal_kernel_package_manifest(package)
    data = verify_metal_kernel_package_manifest(manifest_path)

    assert package.bridge_library_status == STATUS_SKIPPED_WITH_REASON
    assert package.bridge_library_produced is False
    assert "xcrun clang missing" in (package.bridge_library_reason or "")
    assert "bridge.library" not in data["artifacts"]


def test_metal_kernel_package_rejects_bridge_dylib_link_errors(tmp_path):
    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge object")
        return output_path

    def rejecting_linker(output_path, *, object_path=None, timeout=30.0):
        raise MetalCompileError("duplicate symbol")

    with pytest.raises(MetalPackageError, match="duplicate symbol"):
        build_metal_kernel_package(
            _module(),
            _packed_args(),
            tmp_path,
            compile_bridge=True,
            link_bridge_library=True,
            bridge_compiler=fake_compiler,
            bridge_linker=rejecting_linker,
        )


def test_metal_kernel_package_can_validate_bridge_dylib_symbol_without_calling(tmp_path):
    calls = {}

    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge object")
        return output_path

    def fake_linker(output_path, *, object_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge dylib")
        return output_path

    def fake_loader(library_path, *, symbol):
        calls["library_path"] = library_path
        calls["symbol"] = symbol
        assert library_path.is_file()
        return symbol

    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        bridge_compiler=fake_compiler,
        bridge_linker=fake_linker,
        bridge_loader=fake_loader,
    )
    data = package.to_dict()

    assert package.bridge_library_load_status == STATUS_BRIDGE_LIBRARY_LOAD_VALIDATED
    assert package.bridge_library_load_validated is True
    assert package.runtime_launch_executed is False
    assert calls["library_path"] == tmp_path / "gemm_kernel_metal_bridge.dylib"
    assert calls["symbol"] == "__pcc_launch_gemm_kernel_metal_runtime_bridge"
    assert data["bridge_library"]["symbol"] == calls["symbol"]
    assert data["bridge_library"]["load_validated"] is True
    assert "not called" in data["bridge_library"]["load_reason"]


def test_metal_kernel_package_requires_dylib_before_load_validation(tmp_path):
    with pytest.raises(MetalPackageError, match="requires link_bridge_library=True"):
        build_metal_kernel_package(
            _module(),
            _packed_args(),
            tmp_path,
            validate_bridge_library=True,
        )


def test_metal_kernel_package_rejects_bridge_dylib_missing_symbol(tmp_path):
    def fake_compiler(output_path, *, source_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge object")
        return output_path

    def fake_linker(output_path, *, object_path=None, timeout=30.0):
        output_path.write_bytes(b"fake bridge dylib")
        return output_path

    def missing_symbol_loader(library_path, *, symbol):
        raise MetalCompileError(f"dynamic library symbol not found: {symbol}")

    with pytest.raises(MetalPackageError, match="symbol not found"):
        build_metal_kernel_package(
            _module(),
            _packed_args(),
            tmp_path,
            compile_bridge=True,
            link_bridge_library=True,
            validate_bridge_library=True,
            bridge_compiler=fake_compiler,
            bridge_linker=fake_linker,
            bridge_loader=missing_symbol_loader,
        )


def test_metal_kernel_package_manifest_rejects_tampered_artifact(tmp_path):
    package = build_metal_kernel_package(_module(), _packed_args(), tmp_path)
    manifest_path = write_metal_kernel_package_manifest(package)

    (tmp_path / "gemm_kernel_metal_bridge.m").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(MetalPackageError, match="bridge.source.*changed"):
        verify_metal_kernel_package_manifest(manifest_path)


def test_metal_kernel_package_manifest_rejects_runtime_launch_claim(tmp_path):
    package = build_metal_kernel_package(_module(), _packed_args(), tmp_path)
    manifest_path = write_metal_kernel_package_manifest(package)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["runtime_launch_executed"] = True
    manifest_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    with pytest.raises(MetalPackageError, match="runtime_launch_executed"):
        verify_metal_kernel_package_manifest(manifest_path)


def test_metal_kernel_package_can_record_nonexecuting_bridge_invocation_packet(tmp_path):
    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        build_invocation_packet=True,
        bridge_compiler=_fake_bridge_compiler,
        bridge_linker=_fake_bridge_linker,
        bridge_loader=_fake_bridge_loader,
    )
    packet = package.bridge_invocation_packet
    assert packet is not None
    data = packet.to_dict()

    assert data["status"] == STATUS_BRIDGE_INVOCATION_PACKET_NOT_READY
    assert data["symbol"] == "__pcc_launch_gemm_kernel_metal_runtime_bridge"
    assert data["bridge_library_path"] == str(tmp_path / "gemm_kernel_metal_bridge.dylib")
    assert data["metallib_available"] is False
    assert data["runtime_launch_executed"] is False
    assert data["whole_program_gpu"] is False
    assert data["fence_callback_required"] is True
    assert data["wait_until_completed"] is False
    assert data["native_buffer_handles_ready"] is False
    assert data["native_buffer_binding_status"] is None
    assert data["invocable"] is False
    assert "no produced metallib artifact" in " ".join(data["not_ready_reasons"])
    assert "native id<MTLBuffer>" in " ".join(data["not_ready_reasons"])
    assert [slot["name"] for slot in data["buffer_handle_slots"]] == ["A", "B", "C"]
    assert [slot["kernel_index"] for slot in data["buffer_handle_slots"]] == [0, 1, 2]
    assert [slot["bridge_ordinal"] for slot in data["buffer_handle_slots"]] == [0, 1, 2]
    assert all(slot["native_mtlbuffer_bound"] is False for slot in data["buffer_handle_slots"])
    assert data["scalar_value_slots"] == []

    manifest_path = write_metal_kernel_package_manifest(package)
    manifest = verify_metal_kernel_package_manifest(manifest_path)
    assert manifest["package"]["bridge_invocation_packet"]["symbol"] == data["symbol"]
    assert manifest["package"]["bridge_invocation_packet"]["invocable"] is False


def test_bridge_invocation_packet_uses_distinct_native_mtlbuffer_bindings(tmp_path):
    package = _validated_bridge_package(tmp_path)
    native_ptrs = _native_ptrs_for_plan(package.launch_plan)
    binding_set = build_metal_native_buffer_binding_set(
        package.launch_plan,
        native_ptrs,
    )
    packet = build_metal_bridge_invocation_packet(
        package,
        native_buffer_bindings=binding_set,
    )
    data = packet.to_dict()

    assert binding_set.status == STATUS_NATIVE_BUFFER_BINDINGS_READY
    assert data["native_buffer_handles_ready"] is True
    assert data["native_buffer_binding_status"] == STATUS_NATIVE_BUFFER_BINDINGS_READY
    assert data["invocable"] is False
    assert data["not_ready_reasons"] == ["no produced metallib artifact is available"]
    for slot in data["buffer_handle_slots"]:
        assert slot["native_mtlbuffer_bound"] is True
        assert slot["native_mtlbuffer_ptr"] == native_ptrs[slot["handle_id"]]
        assert slot["source"] == "runtime-supplied id<MTLBuffer> pointer"


def test_native_mtlbuffer_binding_set_rejects_mismatched_handles(tmp_path):
    package = _validated_bridge_package(tmp_path)
    buffer_handle_ids = [
        arg.handle_id for arg in package.launch_plan.args if arg.kind == "buffer"
    ]

    with pytest.raises(MetalNativeBufferBindingError, match="missing native MTLBuffer"):
        build_metal_native_buffer_binding_set(package.launch_plan, {})

    bad_zero = {handle_id: 0x100000 for handle_id in buffer_handle_ids}
    bad_zero[buffer_handle_ids[0]] = 0
    with pytest.raises(MetalNativeBufferBindingError, match="non-zero int"):
        build_metal_native_buffer_binding_set(package.launch_plan, bad_zero)

    extra = {handle_id: 0x100000 for handle_id in buffer_handle_ids}
    extra[999999] = 0x200000
    with pytest.raises(MetalNativeBufferBindingError, match="non-launch handle"):
        build_metal_native_buffer_binding_set(package.launch_plan, extra)


def test_metal_kernel_package_manifest_rejects_packet_runtime_claim(tmp_path):
    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        build_invocation_packet=True,
        bridge_compiler=_fake_bridge_compiler,
        bridge_linker=_fake_bridge_linker,
        bridge_loader=_fake_bridge_loader,
    )
    manifest_path = write_metal_kernel_package_manifest(package)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["package"]["bridge_invocation_packet"]["runtime_launch_executed"] = True
    manifest_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    with pytest.raises(MetalPackageError, match="bridge invocation packet"):
        verify_metal_kernel_package_manifest(manifest_path)


def test_metal_kernel_package_bridge_invocation_packet_records_scalar_slots(tmp_path):
    package = build_metal_kernel_package(
        _copy_module(),
        _copy_packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        validate_bridge_library=True,
        build_invocation_packet=True,
        wait_until_completed=True,
        bridge_compiler=_fake_bridge_compiler,
        bridge_linker=_fake_bridge_linker,
        bridge_loader=_fake_bridge_loader,
    )
    data = package.bridge_invocation_packet.to_dict()

    assert [slot["name"] for slot in data["buffer_handle_slots"]] == ["src", "dst"]
    assert data["scalar_value_slots"] == [
        {
            "bridge_ordinal": 0,
            "kernel_index": 2,
            "name": "n",
            "dtype": "u32",
            "scalar_value": 16,
            "source": "POD scalar copied through scalar_values pointer slot",
        }
    ]
    assert data["wait_until_completed"] is True
    assert data["runtime_launch_executed"] is False


def test_bridge_invocation_packet_requires_validated_dylib_symbol(tmp_path):
    package = build_metal_kernel_package(
        _module(),
        _packed_args(),
        tmp_path,
        compile_bridge=True,
        link_bridge_library=True,
        bridge_compiler=_fake_bridge_compiler,
        bridge_linker=_fake_bridge_linker,
    )

    with pytest.raises(MetalPackageError, match="requires validate_bridge_library=True"):
        build_metal_bridge_invocation_packet(package)

    with pytest.raises(MetalPackageError, match="requires validate_bridge_library=True"):
        build_metal_kernel_package(
            _module(),
            _packed_args(),
            tmp_path / "package_builder",
            compile_bridge=True,
            link_bridge_library=True,
            build_invocation_packet=True,
            bridge_compiler=_fake_bridge_compiler,
            bridge_linker=_fake_bridge_linker,
        )


def test_bridge_invocation_packet_strict_mode_rejects_missing_metallib(tmp_path):
    with pytest.raises(MetalPackageError, match="requires produced metallib"):
        build_metal_kernel_package(
            _module(),
            _packed_args(),
            tmp_path,
            compile_bridge=True,
            link_bridge_library=True,
            validate_bridge_library=True,
            build_invocation_packet=True,
            allow_missing_metallib=False,
            bridge_compiler=_fake_bridge_compiler,
            bridge_linker=_fake_bridge_linker,
            bridge_loader=_fake_bridge_loader,
        )
