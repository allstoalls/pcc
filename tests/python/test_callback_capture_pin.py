"""Owned native PyFunc captures/result alias ABI; explicit host producer.

This deliberately installs the compiler's continuation-factory role through
the public header prefix. The C header exposes flags but not that role macro;
its fixture value is checked against the owned Python ABI table below. It is
not a claim about a pure Python or CPython PyCFunction reproduction.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/native/callback_capture_pin/capture_pin.c"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "darwin" or platform.machine() != "arm64",
                       reason="this owned C/runtime ABI gate selects macOS arm64"),
]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _run(command, environment, directory, prefix):
    (directory / (prefix + ".command.json")).write_text(json.dumps(command, indent=2) + "\n")
    result = subprocess.run(command, cwd=directory, env=environment,
                            capture_output=True, text=True, timeout=90)
    (directory / (prefix + ".stdout")).write_text(result.stdout)
    (directory / (prefix + ".stderr")).write_text(result.stderr)
    (directory / (prefix + ".status.json")).write_text(json.dumps({"returncode": result.returncode}) + "\n")
    return result


@pytest.fixture(scope="module")
def capture_pin_program(tmp_path_factory):
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

    runtime_value = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert runtime_value, "select an explicit matching runtime; no implicit build"
    runtime = Path(runtime_value).resolve(strict=True)
    verify_runtime_archive_manifest(runtime, runtime_root=ROOT / "pcc/runtime")
    constants = {}
    tree = ast.parse((ROOT / "pcc/runtime/py/py_abi_constants.py").read_text())
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = statement.value.value
    assert constants["PY_FLAG_GC_PINNED"] == 64
    assert constants["PY_FLAG_FUNC_CONTINUATION_FACTORY"] == 67108864
    directory = tmp_path_factory.mktemp("callback_capture_pin")
    source = directory / "capture_pin.c"
    source.write_bytes(FIXTURE.read_bytes())
    obj, binary = directory / "capture_pin.o", directory / "capture_pin"
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    environment.update(PYTHONPATH=str(ROOT), PCC_SOURCE_ROOT=str(ROOT), PCC_REPO_ROOT=str(ROOT),
                       PCC_RUNTIME_ARCHIVE=str(runtime), PCC_RUNTIME_CC="/usr/bin/false",
                       PCC_SELF_LINK="pcc", PCC_SELF_OBJ="pcc", PCC_GC_BACKEND="0",
                       PCC_WITH_THREADS="1", PCC_REFCOUNT_KIND="atomic", PCC_NO_AUTO_PCC1="1",
                       PCC_PY_FRONTEND_IR_CACHE="0", PCC_SELF_BACKEND_OBJECT_CACHE="0")
    identities = {str(path): _sha(path) for path in (source, runtime)}
    built = _run([sys.executable, "-P", "-m", "pcc", "--backend", "self", "--no-cache", "-O0",
                  "--cpp-arg=-I" + str(ROOT / "pcc/runtime/include"),
                  "--emit-obj", str(obj), str(source)], environment, directory, "compile-host-owned")
    assert built.returncode == 0, built.stdout + built.stderr
    linked = _run([sys.executable, "-P", "-m", "pcc.backend.owned_link_driver", "--target",
                   "arm64-apple-darwin", "--out", str(binary), "--object", str(obj),
                   "--archive", str(runtime)], environment, directory, "link-host-owned")
    assert linked.returncode == 0, linked.stdout + linked.stderr
    assert all(_sha(path) == digest for path, digest in identities.items())
    identities[str(binary)] = _sha(binary)
    (directory / "identities.json").write_text(json.dumps(identities, indent=2) + "\n")
    return binary, environment, identities


@pytest.mark.parametrize("backend", (1, 4), ids=("gc1", "gc4"))
@pytest.mark.parametrize("prior_pin", (0, 1), ids=("unpinned", "already-pinned"))
@pytest.mark.parametrize("factory", (1, 0), ids=("sync-factory", "ordinary-entry"))
def test_host_owned_native_capture_pin_is_restored(capture_pin_program, backend, prior_pin, factory):
    binary, environment, identities = capture_pin_program
    assert all(_sha(path) == digest for path, digest in identities.items())
    result = _run([str(binary), str(backend), str(prior_pin), str(factory)],
                  dict(environment, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"), binary.parent,
                  "execute-" + str(backend) + "-" + str(prior_pin) + "-" + str(factory))
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    expected_pin = 64 if prior_pin else 0
    assert result.stdout == (f"factory={factory} prior={prior_pin} before={expected_pin} "
                             f"after={expected_pin} identity=1 metric_delta=0\n")
    assert all(_sha(path) == digest for path, digest in identities.items())
