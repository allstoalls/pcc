from pcc.py_frontend import pipeline, pipeline_frontend_workers as workers
import pytest


def test_direct_phi_still_rejects_an_unrelated_live_nonpredecessor(monkeypatch):
    from pcc.llvm_capi import ir
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_verify import verify_parsed_module

    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="invalid_edge")
    function = ir.Function(module, ir.FunctionType(ir.IntType(32), []), name="main")
    entry = function.append_basic_block("entry")
    right = function.append_basic_block("right")
    join = function.append_basic_block("join")
    builder = ir.IRBuilder(entry)
    builder.branch(right)
    builder.position_at_end(right)
    builder.branch(join)
    builder.position_at_end(join)
    phi = builder.phi(ir.IntType(32), name="result")
    phi.add_incoming(ir.Constant(ir.IntType(32), 7), entry)
    phi.add_incoming(ir.Constant(ir.IntType(32), 42), right)
    builder.ret(phi)
    with pytest.raises(BackendUnavailable, match="non-predecessor"):
        verify_parsed_module(module.direct_indexed_module())


@pytest.mark.parametrize("condition", [False, True])
@pytest.mark.parametrize("fuse", [False, True])
def test_folded_conditional_edge_is_removed_from_a_live_join_phi(tmp_path, monkeypatch, condition, fuse):
    import subprocess
    from pcc.llvm_capi import ir
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_module
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.native_object import NativeObject
    from pcc.backend.macho_exec import link_executable

    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1" if fuse else "0")
    module = ir.Module(name="constant_edge")
    function = ir.Function(module, ir.FunctionType(ir.IntType(32), []), name="main")
    entry = function.append_basic_block("entry")
    right = function.append_basic_block("right")
    join = function.append_basic_block("join")
    builder = ir.IRBuilder(entry)
    builder.cbranch(ir.Constant(ir.IntType(1), int(condition)), right if condition else join, join if condition else right)
    builder.position_at_end(right)
    builder.branch(join)
    builder.position_at_end(join)
    phi = builder.phi(ir.IntType(32), name="result")
    phi.add_incoming(ir.Constant(ir.IntType(32), 7), entry)
    phi.add_incoming(ir.Constant(ir.IntType(32), 42), right)
    builder.ret(phi)
    assembly = emit_aarch64_darwin_indexed_module(module.direct_indexed_module(), optimize=False)
    sections, undefined = assemble_file(assembly)
    output = tmp_path / "program"
    output.write_bytes(link_executable([NativeObject.from_sections(sections, undefined=undefined)], entry="_main"))
    output.chmod(0o755)
    result = subprocess.run([str(output)], capture_output=True, timeout=10)
    assert result.returncode == 42, result.stderr


def test_direct_short_circuit_phi_after_a_fallible_call(tmp_path, monkeypatch):
    from pcc.llvm_capi import ir
    from pcc.py_frontend.codegen import exception_lowering

    monkeypatch.setattr(ir, "_DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED", True)
    monkeypatch.setattr(exception_lowering, "_DIRECT_INLINE_ERROR_EDGE_ENABLED", True)
    for name in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK", "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES"):
        monkeypatch.setenv(name, "1")
    source = tmp_path / "condition.py"
    source.write_text('''
class Reader:
    def ensure_open(self):
        if self.closed:
            raise ValueError("closed")
    def __init__(self):
        self.closed = False
        self.buffer = b"abc"
        self.eof = False
    def read(self, size=-1):
        self.ensure_open()
        if size is None or int(size) < 0:
            return 42
        wanted = int(size)
        while len(self.buffer) < wanted and not self.eof:
            self.eof = True
        return size
def main():
    r = Reader()
    print(r.read(None))
    print(r.read(-1))
    print(r.read(3))
main()
''')
    manifest = tmp_path / "worker.manifest"
    workers.write_worker_manifest(str(manifest), str(tmp_path / "result.tsv"), str(tmp_path), "", "",
                                  [str(source)], ["condition"], [0], entry_module="condition", sibling_inits=(),
                                  libpython_mode="off", ir_scaffold_mode="on", verbose=False)
    assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, (tmp_path / "result.tsv").read_text()
