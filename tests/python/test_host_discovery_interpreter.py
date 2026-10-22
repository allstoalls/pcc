"""Host dependency probes follow the invoking Python and preserve native isolation."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shlex
import sys
import sysconfig
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_dependency_closure as closure


def _probe(kind):
    if kind == "origin":
        return closure._host_find_spec_origin("json")
    return closure._host_sysconfig_roots(["stdlib", "platstdlib"])


def _expected(kind):
    if kind == "origin":
        return importlib.util.find_spec("json").origin
    paths = sysconfig.get_paths()
    return list(dict.fromkeys(
        os.path.realpath(os.path.abspath(paths[key]))
        for key in ("stdlib", "platstdlib") if paths.get(key)
    ))


def _shim(path: Path, marker: Path, *, delegate: bool):
    body = "#!/bin/sh\nprintf 'probe\\n' >> " + shlex.quote(str(marker)) + "\n"
    if delegate:
        body += "exec " + shlex.quote(sys.executable) + ' "$@"\n'
    else:
        body += "exit 19\n"
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("kind", ["origin", "roots"])
@pytest.mark.parametrize("empty_override", [False, True])
def test_host_default_uses_actual_interpreter_with_conflicting_path(
    monkeypatch, tmp_path, kind, empty_override,
):
    shadow_marker = tmp_path / "shadow-was-run"
    _shim(tmp_path / "python3", shadow_marker, delegate=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    if empty_override:
        monkeypatch.setenv("PCC_HOST_PYTHON", "")
    else:
        monkeypatch.delenv("PCC_HOST_PYTHON", raising=False)
    monkeypatch.setattr(closure, "_HOST_FIND_SPEC_ORIGIN_CACHE", {})

    assert _probe(kind) == _expected(kind)
    assert not shadow_marker.exists()


@pytest.mark.parametrize("kind", ["origin", "roots"])
def test_explicit_host_interpreter_is_actually_executed(monkeypatch, tmp_path, kind):
    shadow_marker = tmp_path / "shadow-was-run"
    override_marker = tmp_path / "override-was-run"
    _shim(tmp_path / "python3", shadow_marker, delegate=False)
    override = _shim(tmp_path / "selected-python", override_marker, delegate=True)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PCC_HOST_PYTHON", str(override))
    monkeypatch.setattr(closure, "_HOST_FIND_SPEC_ORIGIN_CACHE", {})

    assert _probe(kind) == _expected(kind)
    assert override_marker.read_text().splitlines() == ["probe"]
    assert not shadow_marker.exists()


def test_probe_cache_reuses_same_interpreter_and_separates_override(monkeypatch, tmp_path):
    first_marker, second_marker = tmp_path / "first.log", tmp_path / "second.log"
    first = _shim(tmp_path / "first-python", first_marker, delegate=True)
    second = _shim(tmp_path / "second-python", second_marker, delegate=True)
    monkeypatch.setattr(closure, "_HOST_FIND_SPEC_ORIGIN_CACHE", {})
    monkeypatch.setenv("PCC_HOST_PYTHON", str(first))
    assert _probe("origin") == _probe("origin") == _expected("origin")
    assert first_marker.read_text().splitlines() == ["probe"]
    monkeypatch.setenv("PCC_HOST_PYTHON", str(second))
    assert _probe("origin") == _expected("origin")
    assert second_marker.read_text().splitlines() == ["probe"]
    monkeypatch.setenv("PCC_HOST_PYTHON", str(first))
    assert _probe("origin") == _expected("origin")
    assert first_marker.read_text().splitlines() == ["probe"]


@pytest.mark.parametrize("explicit_override", [False, True])
def test_native_guards_precede_host_interpreter_resolution(monkeypatch, tmp_path, explicit_override):
    marker = tmp_path / "native-must-not-probe"
    selected = _shim(tmp_path / "selected-python", marker, delegate=True)
    if explicit_override:
        monkeypatch.setenv("PCC_HOST_PYTHON", str(selected))
    else:
        monkeypatch.delenv("PCC_HOST_PYTHON", raising=False)
    # No executable attribute: even resolving the new host default here is wrong.
    monkeypatch.setattr(closure, "sys", SimpleNamespace(
        implementation=SimpleNamespace(name="pcc"),
    ))
    assert _probe("origin") == ""
    assert _probe("roots") == []
    assert not marker.exists()
