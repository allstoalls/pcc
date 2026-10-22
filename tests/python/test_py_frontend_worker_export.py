from pathlib import Path


def test_export_worker_full_lifts_native_stdlib_asyncio(tmp_path):
    from pcc.frontends.python import pipeline

    repo = None
    for parent in Path(__file__).resolve().parents:
        if (parent / "pcc" / "stdlib" / "asyncio.py").is_file():
            repo = parent
            break
    assert repo is not None
    src = repo / "pcc" / "stdlib" / "asyncio.py"
    result = tmp_path / "export.tsv"
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    manifest = tmp_path / "export.manifest"

    pipeline._write_python_frontend_worker_manifest(
        str(manifest),
        str(result),
        str(export_dir),
        "",
        "",
        [str(src)],
        ["asyncio"],
        [0],
        entry_module="asyncio",
        sibling_inits=(),
        libpython_mode="off",
        ir_scaffold_mode="on",
        verbose=False,
        job_kind="export",
    )

    assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0
    assert result.read_text(encoding="utf-8").startswith("EXPORT\t")
