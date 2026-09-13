"""Direct object workers must apply the requested owned IR transformations."""

import os
import builtins
from pathlib import Path
import subprocess

from pcc.backend import self_backend_aarch64_darwin as emitter
from pcc.backend.macho_exec import link_executable
from pcc.backend.native_object import decode_packed_native_object
from pcc.backend.self_backend_ir import PARSED_INSTRUCTION_KINDS
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.py_frontend import pipeline
from pcc.llvm_capi import ir


def test_direct_worker_promotes_memory_before_pco_emission(
    tmp_path, monkeypatch, pcc_py_runtime_archive,
):
    source = tmp_path / "probe.py"
    source.write_text('''def choose(flag):
    if flag:
        value = 40
    else:
        value = 41
    return value + 1
print(choose(True), choose(False))
''')
    for key in (
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
    ):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1")
    monkeypatch.delenv("PCC_DIRECT_INDEXED_SIDECAR", raising=False)
    monkeypatch.delenv("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", raising=False)
    import_original = builtins.__import__

    def reject_llvm(name, *args, **kwargs):
        if name == "llvmlite" or name.startswith("llvmlite.") or name == "pcc.llvm_capi.binding":
            raise AssertionError("unexpected external optimizer: " + name)
        return import_original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_llvm)
    original = emitter.emit_aarch64_darwin_indexed_transport
    observed = []
    original_capture = ir.Module.direct_indexed_module
    captures = []

    def capture_frontend(module):
        captures.append(module.name)
        return original_capture(module)

    monkeypatch.setattr(ir.Module, "direct_indexed_module", capture_frontend)

    def capture(module, **kwargs):
        counts = {"alloca": 0, "load": 0, "store": 0, "phi": 0}
        for function in module.functions:
            kernel = get_indexed_function_kernel(function)
            for block_id in range(len(kernel.block_names)):
                block = kernel.block_fact(block_id)
                counts["phi"] += kernel.block_phi_fact(block_id).second
                for index in range(block.first, block.first + block.second):
                    kind = PARSED_INSTRUCTION_KINDS[
                        kernel.instruction_metadata_by_id(index).first
                    ]
                    if kind in counts:
                        counts[kind] += 1
        observed.append(counts)
        return original(module, **kwargs)

    monkeypatch.setattr(emitter, "emit_aarch64_darwin_indexed_transport", capture)
    for mode in ("off", "default"):
        captures.clear()
        monkeypatch.setenv("PCC_PYTHON_IR_PASSES", mode)
        output = tmp_path / mode
        output.mkdir()
        manifest = output / "worker.manifest"
        result = output / "result.tsv"
        pipeline._write_python_frontend_worker_manifest(
            str(manifest), str(result), str(output), "", "", [str(source)],
            ["probe"], [0], entry_module="probe", sibling_inits=(),
            libpython_mode="off", ir_scaffold_mode="on", verbose=False,
        )
        assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
        assert len(captures) == (1 if mode == "off" else 0)
        if mode == "off":
            assert (output / "module_0.ll").read_text() == ""
        pco = output / "module_0.direct.pco"
        image = link_executable(
            [decode_packed_native_object(pco.read_bytes())],
            archives=[Path(pcc_py_runtime_archive).read_bytes()],
        )
        binary = output / "program"
        binary.write_bytes(image)
        binary.chmod(0o755)
        for backend in range(5):
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                 env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
            assert ran.returncode == 0, f"{mode} GC{backend}: {ran.stdout}; {ran.stderr}"
            assert ran.stdout == "41 42\n"
    before, after = observed
    assert after["alloca"] < before["alloca"], observed
    assert after["load"] < before["load"], observed
    assert after["store"] < before["store"], observed
    assert after["phi"] > before["phi"], observed
    print("direct emitter input:", before, "->", after)


def test_direct_worker_rejects_unowned_pass_instead_of_ignoring_it(tmp_path, monkeypatch):
    source = tmp_path / "probe.py"
    source.write_text("def value():\n    return 42\nprint(value())\n")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "unsupported-pass")
    manifest = tmp_path / "worker.manifest"
    result = tmp_path / "result.tsv"
    pipeline._write_python_frontend_worker_manifest(
        str(manifest), str(result), str(tmp_path), "", "", [str(source)],
        ["probe"], [0], entry_module="probe", sibling_inits=(),
        libpython_mode="off", ir_scaffold_mode="on", verbose=False,
    )
    assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 1
    assert "self optimizer does not own requested passes: unsupported-pass" in result.read_text()
    assert not (tmp_path / "module_0.direct.pco").exists()
