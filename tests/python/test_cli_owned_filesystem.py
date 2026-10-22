"""Execute CLI filesystem transitions while denying external filesystem tools.

These host driver tests do not certify pcc1; the native CLI matrix must execute
these same entry shapes using a compiler built from the changed source.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_scratch_creation_preserves_collisions_and_does_not_spawn(monkeypatch, tmp_path):
    import pcc.driver.cli_bootstrap as cli

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(cli, "_bootstrap_subprocess_run", lambda *a, **k: pytest.fail("external filesystem owner"))
    collision = tmp_path / ("pcc-test-" + str(os.getpid()) + "-0")
    collision.mkdir()
    sentinel = collision / "keep"
    sentinel.write_text("original")
    first = Path(cli._make_bootstrap_run_tempdir("pcc-test-"))
    second = Path(cli._make_bootstrap_run_tempdir("pcc-test-"))
    assert first != second and first != collision
    (first / "nested").mkdir()
    (first / "nested" / "payload").write_text("content")
    cli._remove_bootstrap_run_tempdir(str(first))
    cli._remove_bootstrap_run_tempdir(str(first))
    assert not first.exists()
    assert second.is_dir()
    assert sentinel.read_text() == "original"
    cli._remove_bootstrap_run_tempdir(str(second))


def test_scratch_cleanup_unlinks_symlink_without_following_it(monkeypatch, tmp_path):
    import pcc.driver.cli_bootstrap as cli

    monkeypatch.setattr(cli, "_bootstrap_subprocess_run", lambda *a, **k: pytest.fail("external filesystem owner"))
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_text("original")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    cli._remove_bootstrap_run_tempdir(str(link))
    assert not link.is_symlink()
    assert sentinel.read_text() == "original"
    link.symlink_to(tmp_path / "missing", target_is_directory=True)
    cli._remove_bootstrap_run_tempdir(str(link))
    assert not link.is_symlink()


def test_cache_publish_and_reuse_require_no_filesystem_subprocess(monkeypatch, tmp_path):
    import pcc.driver.cli_bootstrap as cli

    source = tmp_path / "program.py"
    source.write_text("def main():\n    print(42)\nmain()\n")
    cache = tmp_path / "cache" / "nested" / "program"
    compiled = []
    ran = []

    def compile_program(_source, output, **_kwargs):
        compiled.append(output)
        Path(output).write_text("compiled-output")
        return None

    def run_program(command, *, check):
        assert check is True
        assert command[0] == str(cache)
        assert cache.read_text() == "compiled-output"
        ran.append(command)

    monkeypatch.setattr(cli, "_python_run_cache_path", lambda *a, **k: str(cache))
    monkeypatch.setattr(cli, "_observed_compile_python", compile_program)
    monkeypatch.setattr(cli, "_bootstrap_subprocess_run", run_program)
    assert cli.bootstrap_cli_main([str(source), "arg"]) == 0
    assert cli.bootstrap_cli_main([str(source), "arg"]) == 0
    assert len(compiled) == 1
    assert not Path(compiled[0]).exists()
    assert len(ran) == 2
    assert ran[0] == ran[1]


@pytest.mark.parametrize("kind", ["command", "stdin"])
def test_inline_requests_clean_owned_scratch_without_filesystem_tools(monkeypatch, tmp_path, kind):
    import pcc.driver.cli_bootstrap as cli

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = "def main():\n    print(42)\nmain()\n"
    materialized = []
    ran = []

    def compile_program(path, output, **_kwargs):
        assert Path(path).read_text() == source
        Path(output).write_text("compiled-output")
        materialized.append(path)
        return None

    def run_program(command, *, check):
        assert check is True
        assert Path(command[0]).read_text() == "compiled-output"
        assert command[1:4] == [cli._PYTHON_ARGV0_MARKER, kind, "-c" if kind == "command" else "-"]
        ran.append(command)

    monkeypatch.setattr(cli, "_observed_compile_python", compile_program)
    monkeypatch.setattr(cli, "_bootstrap_subprocess_run", run_program)
    assert cli._run_inline_python_request(kind, source, ["arg"]) == 0
    assert len(ran) == 1
    assert len(materialized) == 1
    assert not Path(materialized[0]).exists()
    assert list(tmp_path.iterdir()) == []
