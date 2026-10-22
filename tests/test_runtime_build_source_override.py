from __future__ import annotations

from pathlib import Path
import shutil
import stat
import sys
from types import SimpleNamespace

import pytest

from pcc.frontends.python import owned_runtime_build
from tests import runtime_build_cache as cache
from tests.test_runtime_archive_consumers import _write_valid_runtime_archive


_SOURCE_FILES = {
    "pcc/runtime/Makefile": "all:\n",
    "pcc/runtime/src/py_tuple.c": "tuple-c\n",
    "pcc/runtime/py/py_tuple.py": "tuple-python\n",
    "pcc/runtime/include/py_runtime.h": "header\n",
    "pcc/frontends/python/lowering.py": "compiler\n",
    "pcc/backend/emit.py": "backend\n",
}


@pytest.fixture
def runtime_sources(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for name, content in reversed(tuple(_SOURCE_FILES.items())):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    runtime = repo / "pcc" / "runtime"
    external = tmp_path / ".frozen-source" / "py_runtime"
    shutil.copytree(runtime, external)
    pcc_bin = repo / ".venv" / "bin" / "pcc"
    pcc_bin.parent.mkdir(parents=True)
    pcc_bin.write_text("fixture compiler launcher\n", encoding="utf-8")
    monkeypatch.setattr(cache, "_REPO_ROOT", repo)
    monkeypatch.setattr(cache, "_RUNTIME_DIR", runtime)
    monkeypatch.setattr(cache, "sys", SimpleNamespace(version="fixture-python", executable=sys.executable))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    for name in cache._PCC_PY_ARCHIVE_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(repo=repo, runtime=runtime, external=external, pcc_bin=pcc_bin)


def test_default_runtime_key_preserves_v4_serialization(runtime_sources):
    # Checked against unchanged HEAD source-key serialization. The canonical
    # compiler checksum binds the cache key separately from this source key.
    assert cache._pcc_runtime_source_key(Path("/frozen/pcc")) == "7a7329e45c09cd208ce0c982"


def test_identical_external_runtime_uses_same_logical_source_key(runtime_sources):
    sources = runtime_sources
    assert cache._pcc_runtime_source_key(
        sources.pcc_bin, runtime_source=sources.external,
    ) == cache._pcc_runtime_source_key(sources.pcc_bin)
    assert cache._pcc_runtime_cache_key(
        sources.pcc_bin, variant="pcc-py", runtime_source=sources.external,
    ) == cache._pcc_runtime_cache_key(sources.pcc_bin, variant="pcc-py")


def test_runtime_override_hashes_selected_tuple_and_current_compiler(runtime_sources):
    sources = runtime_sources
    original = cache._pcc_runtime_source_key(sources.pcc_bin, runtime_source=sources.external)
    (sources.runtime / "py" / "py_tuple.py").write_text("working-tree variant\n")
    assert cache._pcc_runtime_source_key(sources.pcc_bin, runtime_source=sources.external) == original
    (sources.external / "py" / "py_tuple.py").write_text("tuple control\n")
    tuple_changed = cache._pcc_runtime_source_key(sources.pcc_bin, runtime_source=sources.external)
    assert tuple_changed != original
    (sources.repo / "pcc" / "frontends" / "python" / "lowering.py").write_text("compiler changed\n")
    assert cache._pcc_runtime_source_key(sources.pcc_bin, runtime_source=sources.external) != tuple_changed


@pytest.mark.parametrize("threaded", (False, True))
def test_runtime_override_copies_readonly_source_into_writable_verified_staging(
    runtime_sources, monkeypatch, threaded,
):
    # ``threaded`` selects the helper; both build the one threaded runtime.
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    sources = runtime_sources
    selected_tuple = sources.external / "py" / "py_tuple.py"
    selected_tuple.write_text("frozen tuple control\n", encoding="utf-8")
    stale = sources.external / "build_py"
    stale.mkdir()
    (stale / "py_tuple.o").write_bytes(b"stale object")
    (sources.external / "libpy_runtime_pcc_py.a").write_bytes(b"stale archive")
    (sources.external / "libpy_runtime_pcc_py.a.provenance.json").write_text("stale receipt")
    all_sources = [sources.external, *sources.external.rglob("*")]
    old_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in all_sources}
    for path in all_sources:
        path.chmod(0o555 if path.is_dir() else 0o444)
    builds = []

    def fake_make(command, **kwargs):
        staging = Path(command[2])
        builds.append(staging)
        assert staging != sources.external and staging != sources.runtime
        assert (staging / "py" / "py_tuple.py").read_text() == "frozen tuple control\n"
        assert not (staging / "build_py").exists()
        assert not (staging / "libpy_runtime_pcc_py.a").exists()
        assert not (staging / "libpy_runtime_pcc_py.a.provenance.json").exists()
        for path in (staging, *staging.rglob("*")):
            assert path.stat().st_mode & stat.S_IWUSR, path
            if path.is_dir():
                assert path.stat().st_mode & stat.S_IXUSR, path
        assert f"PCC={sources.pcc_bin}" in command
        assert f"PCC_REPO_ROOT={sources.repo}" in command
        assert "PCC_WITH_THREADS=1" in command
        assert kwargs["timeout"] == 900
        _write_valid_runtime_archive(staging)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cache, "subprocess", SimpleNamespace(run=fake_make))
    builder = cache.cached_threaded_pcc_python_runtime if threaded else cache.cached_pcc_python_runtime
    try:
        runtime = builder(runtime_source=sources.external)
        assert builder(runtime_source=sources.external) == runtime
        assert len(builds) == 1
        assert selected_tuple.read_text() == "frozen tuple control\n"
        assert stat.S_IMODE(selected_tuple.stat().st_mode) == 0o444
        assert stat.S_IMODE(sources.external.stat().st_mode) == 0o555
        archive = runtime / "libpy_runtime_pcc_py.a"
        cache.verify_runtime_archive_manifest(archive, runtime_root=runtime)
        variant = "pcc-py"
        assert cache._pcc_runtime_cache_is_complete(
            runtime=runtime, archive=archive,
            marker=runtime / (".pcc-" + variant + "-complete"),
            key=cache._pcc_runtime_cache_key(sources.pcc_bin, variant=variant, runtime_source=sources.external),
        )
    finally:
        for path, mode in old_modes.items():
            path.chmod(mode)


def test_runtime_cache_key_binds_shared_codegen_identity(runtime_sources, monkeypatch):
    monkeypatch.setattr(cache, "codegen_checksum", lambda: "1" * 64)
    before = cache._pcc_runtime_cache_key(runtime_sources.pcc_bin, variant="pcc-py")
    monkeypatch.setattr(cache, "codegen_checksum", lambda: "2" * 64)
    after = cache._pcc_runtime_cache_key(runtime_sources.pcc_bin, variant="pcc-py")
    assert before != after


@pytest.mark.parametrize("value", (None, "", "1", "true", "yes", "on", " YES "))
def test_default_runtime_forwards_effective_thread_configuration(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    else:
        monkeypatch.setenv("PCC_WITH_THREADS", value)
    expected = owned_runtime_build.runtime_build_config()["threads"]
    assert expected is True
    source = tmp_path / "selected-runtime"
    result = tmp_path / "cached-runtime"
    calls = []

    def record_build(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(cache, "_cached_pcc_python_runtime", record_build)
    assert cache.cached_pcc_python_runtime(runtime_source=source) is result
    assert calls == [{"threaded": expected, "runtime_source": source}]


@pytest.mark.parametrize("value", ("0", "false", "no", "off"))
def test_single_thread_runtime_request_fails_explicitly(monkeypatch, value):
    monkeypatch.setenv("PCC_WITH_THREADS", value)
    with pytest.raises(ValueError, match="single-thread runtime was removed"):
        owned_runtime_build.runtime_build_config()


@pytest.mark.parametrize("threaded", (True,))
def test_default_runtime_follows_configuration_default_changes(monkeypatch, threaded):
    monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    monkeypatch.setattr(
        owned_runtime_build, "runtime_build_config",
        lambda: {"threads": threaded, "refcount": "atomic"},
    )
    calls = []
    result = Path("selected-default-cache")

    def record_build(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(cache, "_cached_pcc_python_runtime", record_build)
    assert cache.cached_pcc_python_runtime() is result
    assert calls == [{"threaded": threaded, "runtime_source": None}]


def test_threaded_runtime_helper_uses_the_one_build(tmp_path, monkeypatch):
    monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    calls = []
    source = tmp_path / "selected-runtime"
    result = tmp_path / "threaded-cache"

    def record_build(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(cache, "_cached_pcc_python_runtime", record_build)
    assert cache.cached_threaded_pcc_python_runtime(runtime_source=source) is result
    assert calls == [{"threaded": True, "runtime_source": source}]


def test_every_thread_setting_selects_the_one_threaded_cache_entry(runtime_sources, monkeypatch):
    # Exercise real cache keys, staging, provenance and publication. Only Make
    # is replaced by tiny host-emitted archive fixtures, not a runtime build.
    # Every runtime is threaded: one entry serves both helpers, and asking for
    # the removed single-thread runtime fails explicitly.
    sources = runtime_sources
    builds = []

    def fake_make(command, **kwargs):
        thread_arguments = [arg for arg in command if arg.startswith("PCC_WITH_THREADS=")]
        assert thread_arguments == ["PCC_WITH_THREADS=1"]
        builds.append(thread_arguments[0])
        assert kwargs["timeout"] == 900
        _write_valid_runtime_archive(Path(command[2]), return_value=len(builds))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cache, "subprocess", SimpleNamespace(run=fake_make))
    monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    default = cache.cached_pcc_python_runtime(runtime_source=sources.external)
    assert default.name.endswith("-pcc-py")
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    assert cache.cached_pcc_python_runtime(runtime_source=sources.external) == default
    assert cache.cached_threaded_pcc_python_runtime(runtime_source=sources.external) == default
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    with pytest.raises(ValueError, match="single-thread runtime was removed"):
        cache.cached_pcc_python_runtime(runtime_source=sources.external)
    assert builds == ["PCC_WITH_THREADS=1"]
