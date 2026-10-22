"""Compiler artifact copies use owned file operations on every host."""

from pcc.frontends.python import pipeline_frontend_parallel as parallel


def test_frontend_artifact_tree_keeps_names_and_binary_contents(tmp_path, monkeypatch):
    def reject_external(*_args, **_kwargs):
        raise AssertionError("compiler artifact copy invoked an external tool")

    monkeypatch.setattr(parallel.subprocess, "run", reject_external)
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "module 中文 with spaces"
    nested.mkdir()
    (source / "empty.ast").write_bytes(b"")
    payload = bytes(range(256)) * 8193
    (nested / "syntax.ast").write_bytes(payload)
    destination = tmp_path / "persistent" / "ast"
    parallel._copy_frontend_artifact_tree(str(source), str(destination))
    assert (destination / "empty.ast").read_bytes() == b""
    assert (destination / nested.name / "syntax.ast").read_bytes() == payload
    assert (nested / "syntax.ast").read_bytes() == payload


def test_frontend_artifact_file_replaces_contents(tmp_path, monkeypatch):
    def reject_external(*_args, **_kwargs):
        raise AssertionError("compiler artifact copy invoked an external tool")

    monkeypatch.setattr(parallel.subprocess, "run", reject_external)
    source = tmp_path / "exports.json"
    destination = tmp_path / "persistent_exports.json"
    source.write_bytes(b'{"module": "value"}\n')
    destination.write_bytes(b"old value" * 100)
    parallel._copy_frontend_artifact_file(str(source), str(destination))
    assert destination.read_bytes() == source.read_bytes()
