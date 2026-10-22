"""Native GC1/2 gray accounting, including writes and terminal decref.

Select this integration gate with PCC_GC_GRAY_COUNT_COMPILER naming an actual
pcc1/pcc2/pcc3 executable and PCC_RUNTIME_ARCHIVE naming its matching runtime.
There is no host-compiler substitute or implicit bootstrap. Run under the
standard performance lock, process-tree watchdog and RSS cap.

These are single-thread state-transition probes, not a complete collection or
threaded GC2 qualification. Every fixture preserves the observed counter,
does not change collector thresholds, and reports without managed allocation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "native" / "gc_gray_count"
_SHAPES = ("mark_free", "root_barrier", "owner_barrier", "inactive_barriers")
_STATUS = {
    10: "first shade did not add exactly one",
    11: "repeat shade added again",
    12: "terminal free did not return counter to baseline",
    13: "freed object remains known",
    14: "mark did not set GRAY",
    20: "inactive barrier changed count",
    21: "inactive barrier changed flags",
    22: "inactive barrier fabricated an active cycle",
    100: "wrong backend",
    101: "threads enabled",
    102: "allocation failed",
    103: "invalid initial object precondition",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command, *, directory, environment, prefix, timeout):
    """Keep the actual command and stdout/stderr beside the emitted program."""
    receipt = {
        "command": command,
        "cwd": str(directory),
        "compiler_owner": "explicit-native-executable",
        "environment": {
            key: value for key, value in environment.items()
            if key.startswith("PCC_") or key == "PATH"
        },
    }
    (directory / (prefix + ".json")).write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    try:
        result = subprocess.run(
            command, cwd=directory, env=environment,
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        (directory / (prefix + ".stdout")).write_bytes(error.stdout or b"")
        (directory / (prefix + ".stderr")).write_bytes(error.stderr or b"")
        raise
    (directory / (prefix + ".stdout")).write_bytes(result.stdout)
    (directory / (prefix + ".stderr")).write_bytes(result.stderr)
    receipt["returncode"] = result.returncode
    (directory / (prefix + ".json")).write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    return result


@pytest.fixture(scope="module")
def gray_count_native_toolchain(request):
    requested = os.environ.get("PCC_GC_GRAY_COUNT_COMPILER", "")
    assert requested, "set PCC_GC_GRAY_COUNT_COMPILER to a native pcc executable"
    compiler = Path(requested).resolve(strict=True)
    assert compiler.is_file() and os.access(compiler, os.X_OK), compiler
    requested_runtime = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert requested_runtime, "set matching PCC_RUNTIME_ARCHIVE; no implicit runtime build"
    archive = Path(requested_runtime).resolve(strict=True)
    assert archive.is_file(), archive
    # Validate the supplied production archive against this source tree through
    # the shared fixture. Requiring the environment above prevents auto-build.
    checked_archive = request.getfixturevalue("pcc_runtime_archive")
    assert Path(checked_archive).resolve() == archive
    return compiler, archive


@pytest.fixture(scope="module", params=_SHAPES, ids=_SHAPES)
def gray_count_native_probe(request, tmp_path_factory, gray_count_native_toolchain):
    compiler, archive = gray_count_native_toolchain
    shape = request.param
    directory = tmp_path_factory.mktemp("gc_gray_count_" + shape)
    source = directory / (shape + ".py")
    source.write_bytes((_FIXTURES / (shape + ".py")).read_bytes())
    executable = directory / (shape + (".exe" if os.name == "nt" else ""))
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    environment.update(
        PCC_SOURCE_ROOT=str(_REPO_ROOT),
        PCC_REPO_ROOT=str(_REPO_ROOT),
        PCC_RUNTIME_ARCHIVE=str(archive),
        PCC_RUNTIME_HIGH="py",
        PCC_RUNTIME_CC=str(directory / "forbidden-host-cc"),
        PCC_HOST_PYTHON=str(directory / "forbidden-host-python"),
        PCC_NO_AUTO_PCC1="1",
        PCC_SELF_LINK="pcc",
        PCC_WITH_THREADS="0",
        PCC_GC_BACKEND="0",
        PCC_PYTHON_IR_PASSES="off",
        PCC_SELF_BACKEND_OBJECT_CACHE="off",
    )
    identities = {str(path): _sha256(path) for path in (compiler, archive, source)}
    result = _run(
        [str(compiler), "--backend", "self", "--python-libpython", "off",
         "--ir-scaffold", "on", str(source), "-o", str(executable)],
        directory=directory, environment=environment, prefix="compile", timeout=90,
    )
    assert result.returncode == 0, (shape, result.stdout, result.stderr)
    assert executable.is_file(), "native compiler did not emit the executable"
    assert {path: _sha256(Path(path)) for path in identities} == identities
    identities[str(executable)] = _sha256(executable)
    (directory / "identities.json").write_text(
        json.dumps(identities, indent=2) + "\n", encoding="utf-8"
    )
    return executable, environment, identities


@pytest.mark.parametrize("gc_backend", (1, 2), ids=("gc1", "gc2"))
def test_native_gray_count_lifecycle(gray_count_native_probe, gc_backend):
    executable, environment, identities = gray_count_native_probe
    assert {path: _sha256(Path(path)) for path in identities} == identities
    result = _run(
        [str(executable)], directory=executable.parent,
        environment=dict(environment, PCC_GC_BACKEND=str(gc_backend), PATH="/nonexistent"),
        prefix="execute-gc" + str(gc_backend), timeout=20,
    )
    detail = _STATUS.get(result.returncode, "unexpected exit or output")
    assert result.returncode == 0, (gc_backend, detail, result.stdout, result.stderr)
    assert result.stdout == b"000\n", (gc_backend, result.stdout, result.stderr)
    assert {path: _sha256(Path(path)) for path in identities} == identities
