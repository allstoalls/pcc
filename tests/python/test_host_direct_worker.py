"""The host direct worker must use the same instruction owner as pcc1."""

from pcc.backend import self_backend_aarch64_darwin as emitter
from pcc.py_frontend import pipeline, pipeline_frontend_workers as workers


def test_host_worker_emits_final_native_instructions(tmp_path, monkeypatch):
    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
    ):
        monkeypatch.setenv(name, "1")
    source = tmp_path / "probe.py"
    source.write_text("def add(a: int, b: int) -> int:\n    return a + b\nprint(add(20, 22))\n")
    manifest = tmp_path / "worker.manifest"
    workers.write_worker_manifest(
        str(manifest), str(tmp_path / "result.tsv"), str(tmp_path), "", "",
        [str(source)], ["probe"], [0], entry_module="probe", sibling_inits=(),
        libpython_mode="off", ir_scaffold_mode="on", verbose=False,
    )
    observed = []
    emit = emitter.emit_aarch64_darwin_indexed_transport

    def traced(*args, **kwargs):
        result = emit(*args, **kwargs)
        observed.append((kwargs.get("structured_instructions"), result.native_finalized))
        return result

    monkeypatch.setattr(emitter, "emit_aarch64_darwin_indexed_transport", traced)
    assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0
    assert observed == [(True, True)]
    assert (tmp_path / "module_0.direct.pco").is_file()
