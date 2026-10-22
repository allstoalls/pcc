"""A verified runtime's refcount mode survives the sanitized child handoff."""

import subprocess

import pytest

from tests.integration import test_threaded_path_ownership_qualification as path_suite
from tests.test_explicit_runtime_fixture_provenance import (
    _archive,
    _forbidden,
    runtime_tree,
)


@pytest.mark.parametrize("verified, ambient", [("local", "atomic"), ("atomic", "local")])
def test_threaded_child_preserves_verified_refcount_despite_conflicting_environment(
    runtime_tree, tmp_path, monkeypatch, verified, ambient,
):
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setenv("PCC_REFCOUNT_KIND", verified)
    archive = _archive(runtime_tree, threads=True, refcount=verified)
    monkeypatch.setenv("PCC_THREADED_RUNTIME_ARCHIVE", str(archive))
    fixture = path_suite.path_toolchain.__wrapped__()
    toolchain = next(fixture)
    assert toolchain[1] == archive
    assert all(row["runtime_build_config"] == {"threads": True, "refcount": verified}
               for row in toolchain[3].values())
    monkeypatch.setenv("PCC_REFCOUNT_KIND", ambient)
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    monkeypatch.setenv("PCC_UNRELATED_PLAN", "discard-me")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "discard-me")
    monkeypatch.setenv("PYTHONPATH", "discard-me")
    environment = path_suite._environment(toolchain, tmp_path, 4)
    assert environment["PCC_REFCOUNT_KIND"] == verified
    assert environment["PCC_WITH_THREADS"] == "1"
    assert environment["PCC_RUNTIME_ARCHIVE"] == str(archive)
    assert environment["PCC_GC_BACKEND"] == "4"
    assert environment["PCC_SELF_LINK"] == environment["PCC_SELF_OBJ"] == "pcc"
    for key in ("PCC_UNRELATED_PLAN", "DYLD_INSERT_LIBRARIES", "PYTHONPATH"):
        assert key not in environment
    with pytest.raises(StopIteration):
        next(fixture)
