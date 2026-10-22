"""Compare-exchange remains structured under ordinary no-text capture."""
import sys
import platform
import pytest

from pcc.backend import BackendUnavailable
from pcc.ir import ir
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module


def _module(monkeypatch, *, direct=True, fuse_uses=True):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1" if direct else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1" if fuse_uses else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK", "1")
    module = ir.Module(name="cmpxchg")
    module.triple = "aarch64-apple-darwin"
    i64 = ir.IntType(64)
    fn = ir.Function(module, ir.FunctionType(i64, [i64.as_pointer(), i64, i64]), name="publish")
    entry = fn.append_basic_block("activation.entry")
    body = fn.append_basic_block("publish")
    builder = ir.IRBuilder(entry)
    builder.branch(body)
    builder.position_at_end(body)
    pair = builder.cmpxchg(*fn.args, "acq_rel", "acquire", name="published")
    previous = builder.extract_value(pair, [0], name="previous")
    success = builder.extract_value(pair, [1], name="success")
    result = builder.select(success, fn.args[2], previous)
    builder.ret(result)
    return module


@pytest.mark.parametrize("fuse_uses", [False, True])
def test_cmpxchg_no_text_capture_matches_text(monkeypatch, fuse_uses):
    text = str(_module(monkeypatch, direct=False))
    source = _module(monkeypatch, fuse_uses=fuse_uses)
    direct = source.direct_indexed_module()
    assert source._direct_indexed_fallback_records == 0
    parsed = parse_self_backend_module(text)
    for module in (direct, parsed):
        verify_parsed_module(module)
    actual = get_indexed_function_kernel(direct.functions[0])
    expected = get_indexed_function_kernel(parsed.functions[0])
    assert actual.diagnostic_instruction(1, 0).data == expected.diagnostic_instruction(1, 0).data
    assert actual.instruction_use_count(1, 0) == 3
    assert [actual.instruction_use_id(1, 0, i) for i in range(3)] == [0, 1, 2]
    records = [r for b in source.functions[0].blocks for r in b._instrs]
    assert all(r._direct_record_id >= 0 and not r.text for r in records)


def test_darwin_libsystem_frontend_worker_zero_fallback(monkeypatch, tmp_path):
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    source = tmp_path / "libsystem_probe.py"
    source.write_text(
        "from pcc.unsafe import cstr, darwin_libsystem_symbol, ptr_is_null\n"
        "def exports_symbol() -> bool:\n"
        "    return not ptr_is_null(darwin_libsystem_symbol(cstr('malloc')))\n"
    )
    original_generate = L1CodeGen.generate

    def generate_for_target(self, module=None):
        self._target_triple = "aarch64-apple-darwin"
        return original_generate(self)

    monkeypatch.setattr(L1CodeGen, "generate", generate_for_target)
    for key in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
                "PCC_DIRECT_INDEXED_NATIVE_OBJECT"):
        monkeypatch.setenv(key, "1")
    for key in ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
                "PCC_DIRECT_INDEXED_SIDECAR"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "off")
    original_capture = ir.Module.direct_indexed_module
    captured = []

    def capture(module):
        result = original_capture(module)
        assert module._direct_indexed_fallback_records == 0
        records = [record for function in module.functions for block in function.blocks
                   for record in block._instrs if record.opname == "cmpxchg"]
        assert records and all(record._direct_record_id >= 0 and not record.text for record in records)
        assert any(block.name.startswith("activation.entry") for function in module.functions
                   for block in function.blocks)
        captured.append(len(records))
        return result

    monkeypatch.setattr(ir.Module, "direct_indexed_module", capture)
    manifest = tmp_path / "worker.manifest"
    result = tmp_path / "result.tsv"
    pipeline._write_python_frontend_worker_manifest(
        str(manifest), str(result), str(tmp_path), "", "", [str(source)],
        ["libsystem_probe"], [0], entry_module="libsystem_probe", sibling_inits=(),
        libpython_mode="off", ir_scaffold_mode="on", verbose=False,
    )
    assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    assert captured
    assert (tmp_path / "module_0.ll").read_text() == ""
    assert (tmp_path / "module_0.direct.pco").stat().st_size > 0


def test_cmpxchg_rejects_mismatched_value_types(monkeypatch):
    module = _module(monkeypatch)
    fn = ir.Function(module, ir.FunctionType(ir.VoidType(), []), name="bad")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    ptr = builder.alloca(ir.IntType(64))
    with pytest.raises(BackendUnavailable, match="cmpxchg operand types disagree"):
        builder.cmpxchg(ptr, ir.Constant(ir.IntType(64), 0),
                        ir.Constant(ir.IntType(32), 1), "acq_rel", "acquire")


@pytest.mark.parametrize("target", ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin"])
def test_cmpxchg_owned_object_and_codec_parity(monkeypatch, tmp_path, target):
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.backend.self_backend_indexed_codec import decode_indexed_module_file, encode_indexed_module_file
    from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object

    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "off")
    text_source = _module(monkeypatch, direct=False)
    text_source.triple = target
    text = str(text_source)
    direct_source = _module(monkeypatch)
    direct_source.triple = target
    direct = direct_source.direct_indexed_module()
    path = tmp_path / "cmpxchg.pidx"
    encode_indexed_module_file(str(path), direct)
    if target.startswith("aarch64"):
        from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm
        assembly = emit_aarch64_darwin_asm(text, optimize=False)
    else:
        assembly = emit_self_asm(text)
    expected = encode_assembly_object(assembly, target)
    for module in (direct, decode_indexed_module_file(str(path))):
        verify_parsed_module(module)
        assert encode_assembly_object(emit_indexed_assembly(module, optimize=False), target) == expected


# requires Linux x86-64 execution
@pytest.mark.pcc_gate(probe=lambda: not (not sys.platform.startswith("linux") or platform.machine() not in ("x86_64", "amd64")))
def test_cmpxchg_owned_linux_execution_success_and_failure(monkeypatch, tmp_path):
    import platform
    import subprocess
    import sys
    from pcc.backend.elf_x86_64 import link_static_executable
    from pcc.backend.owned_elf_link import assemble
    from pcc.backend.target_objects import emit_indexed_assembly

    module = _module(monkeypatch)
    module.triple = "x86_64-unknown-linux-gnu"
    i64 = ir.IntType(64)
    main = ir.Function(module, ir.FunctionType(i64, []), name="main")
    builder = ir.IRBuilder(main.append_basic_block("entry"))
    slot = builder.alloca(i64)
    builder.store(ir.Constant(i64, 7), slot)
    publish = module.functions[0]
    succeeded = builder.call(publish, [slot, ir.Constant(i64, 7), ir.Constant(i64, 19)])
    failed = builder.call(publish, [slot, ir.Constant(i64, 7), ir.Constant(i64, 33)])
    actual = builder.load(slot)
    good = builder.and_(builder.icmp_signed("==", succeeded, ir.Constant(i64, 19)),
                        builder.icmp_signed("==", failed, ir.Constant(i64, 19)))
    good = builder.and_(good, builder.icmp_signed("==", actual, ir.Constant(i64, 19)))
    builder.ret(builder.select(good, ir.Constant(i64, 0), ir.Constant(i64, 1)))
    assembly = emit_indexed_assembly(module.direct_indexed_module(), optimize=False)
    startup = ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n  call main\n  mov rdi, rax\n  mov rax, 60\n  syscall\n"
    image = link_static_executable([assemble(assembly, module.triple), assemble(startup, module.triple)])
    executable = tmp_path / "cmpxchg"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.parametrize("success,failure", [("unordered", "acquire"), ("acq_rel", "release"), ("invalid", "monotonic")])
def test_cmpxchg_rejects_unsupported_ordering(monkeypatch, success, failure):
    module = _module(monkeypatch)
    fn = ir.Function(module, ir.FunctionType(ir.VoidType(), []), name="bad_order")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    ptr = builder.alloca(ir.IntType(64))
    with pytest.raises(BackendUnavailable, match="cmpxchg .* ordering is not supported"):
        builder.cmpxchg(ptr, ir.Constant(ir.IntType(64), 0),
                        ir.Constant(ir.IntType(64), 1), success, failure)
