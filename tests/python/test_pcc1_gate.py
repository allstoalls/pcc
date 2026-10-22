from __future__ import annotations

import os
from pathlib import Path

from pcc1_gate import find_current_pcc1, pcc1_freshness_cutoff


def _write_with_mtime(path: Path, text: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_find_current_pcc1_rejects_candidate_older_than_frontend_sources(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCC_CURRENT_PCC1", raising=False)
    _write_with_mtime(tmp_path / "pcc" / "driver" / "cli_bootstrap.py", "# cli\n", 10.0)
    _write_with_mtime(tmp_path / "pcc" / "__main__.py", "# main\n", 20.0)
    _write_with_mtime(tmp_path / "pcc" / "frontends" / "python" / "type_infer.py", "# infer\n", 30.0)
    _write_with_mtime(tmp_path / "pcc1", "fake pcc1\n", 25.0)

    assert pcc1_freshness_cutoff(tmp_path) == 30.0
    assert find_current_pcc1(tmp_path) is None


def test_find_current_pcc1_accepts_candidate_newer_than_frontend_sources(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCC_CURRENT_PCC1", raising=False)
    _write_with_mtime(tmp_path / "pcc" / "driver" / "cli_bootstrap.py", "# cli\n", 10.0)
    _write_with_mtime(tmp_path / "pcc" / "__main__.py", "# main\n", 20.0)
    _write_with_mtime(tmp_path / "pcc" / "backend" / "self_backend_parse.py", "# parse\n", 30.0)
    pcc1 = tmp_path / "pcc1"
    _write_with_mtime(pcc1, "fake pcc1\n", 35.0)

    assert find_current_pcc1(tmp_path) == pcc1


def test_find_current_pcc1_accepts_newer_bootstrap_build_candidate(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCC_CURRENT_PCC1", raising=False)
    _write_with_mtime(tmp_path / "pcc" / "driver" / "cli_bootstrap.py", "# cli\n", 10.0)
    _write_with_mtime(tmp_path / "pcc1", "stale root pcc1\n", 9.0)
    build_pcc1 = tmp_path / "build" / "bootstrap-new-host" / "pcc1"
    _write_with_mtime(build_pcc1, "fresh build pcc1\n", 20.0)

    assert find_current_pcc1(tmp_path) == build_pcc1


def test_find_current_pcc1_normalizes_parent_directory_repo_argument(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCC_CURRENT_PCC1", raising=False)
    repo = tmp_path / "pcc"
    _write_with_mtime(repo / "pcc" / "__main__.py", "# main\n", 10.0)
    build_pcc1 = repo / "build" / "bootstrap-self-darwin_arm64" / "pcc1"
    _write_with_mtime(build_pcc1, "fresh build pcc1\n", 20.0)

    assert find_current_pcc1(tmp_path) == build_pcc1


def test_find_current_pcc1_rejects_candidate_older_than_runtime_sources(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCC_CURRENT_PCC1", raising=False)
    _write_with_mtime(tmp_path / "pcc" / "driver" / "cli_bootstrap.py", "# cli\n", 10.0)
    _write_with_mtime(tmp_path / "pcc" / "runtime" / "src" / "py_obj.c", "/* runtime */\n", 40.0)
    _write_with_mtime(tmp_path / "pcc1", "fake pcc1\n", 35.0)

    assert pcc1_freshness_cutoff(tmp_path) == 40.0
    assert find_current_pcc1(tmp_path) is None


def test_find_current_pcc1_ignores_cache_and_build_sources(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCC_CURRENT_PCC1", raising=False)
    _write_with_mtime(tmp_path / "pcc" / "driver" / "cli_bootstrap.py", "# cli\n", 10.0)
    _write_with_mtime(tmp_path / "pcc" / "__pycache__" / "stale.py", "# cache\n", 90.0)
    _write_with_mtime(tmp_path / "pcc" / "runtime" / "build_py" / "generated.py", "# build\n", 80.0)
    pcc1 = tmp_path / "pcc1"
    _write_with_mtime(pcc1, "fake pcc1\n", 15.0)

    assert pcc1_freshness_cutoff(tmp_path) == 10.0
    assert find_current_pcc1(tmp_path) == pcc1
