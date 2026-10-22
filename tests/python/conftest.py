"""Shared fixtures for tests/python.

The runtime archive fixtures live in tests/conftest.py so every test tree
(kernel, vthread, integration, ...) links the same immutable archives.
"""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path

import pytest



@pytest.fixture(scope="session")
def pcc_diagnostic_runtime_archive(request):
    """Explicit self-emission overlay for capability tests, not production gates."""
    selected = os.environ.get("PCC_DIAGNOSTIC_RUNTIME_ARCHIVE")
    if not selected:
        return request.getfixturevalue("pcc_runtime_archive")
    archive = Path(selected).resolve(strict=True)
    receipt = json.loads(Path(str(archive) + ".diagnostic.json").read_text())
    assert receipt["schema"] == "pcc.runtime-diagnostic-overlay.v1"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["archive_sha256"]
    root = Path(__file__).resolve().parents[2]
    assert receipt["source_sha256"]
    for source, expected in receipt["source_sha256"].items():
        assert hashlib.sha256((root / source).read_bytes()).hexdigest() == expected
    return archive


@pytest.fixture(scope="session")
def native_pcc1_compiler():
    """Resolve a real native executable for explicitly selected pcc1 tests."""
    from tests.python.pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1
    compiler = find_current_pcc1(Path(__file__).resolve().parents[2])
    if compiler is None:
        skip_or_fail_no_current_pcc1("no current pcc1 for source-program regression")
    with compiler.open("rb") as stream:
        magic = stream.read(4)
    assert magic in (b"\xcf\xfa\xed\xfe", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf") or magic[:2] == b"MZ", (
        f"pcc1 regression requires a native executable, not a launcher: {compiler}"
    )
    return compiler


@pytest.fixture(params=[
    pytest.param("pcc0", id="pcc0"),
    pytest.param("pcc1", id="pcc1", marks=pytest.mark.integration),
])
def python_program_compiler(request):
    """Run the same source-program regression through host and native pcc.

    Native cases are integration tests. PCC_CURRENT_PCC1 selects an isolated
    compiler; the caller still executes and checks every emitted program.
    """
    if request.param == "pcc0":
        from pcc.frontends.python.pipeline import compile_python

        return compile_python

    from tests.python.process_timeout import run_process_group_timeout
    compiler = request.getfixturevalue("native_pcc1_compiler")

    def compile_program(source, output, *, backend, libpython_mode, runtime_archive,
                        ir_scaffold_mode="on"):
        env = dict(os.environ)
        env.pop("LC_ALL", None)
        env.update(
            PCC_RUNTIME_ARCHIVE=str(runtime_archive),
            PCC_RUNTIME_CC="/usr/bin/false",
            PCC_HOST_PYTHON="/usr/bin/false",
            PCC_HOST_PCC="/usr/bin/false",
            PCC_NO_AUTO_PCC1="1",
            # A failing codegen worker then prints its traceback (file and
            # line) after its usual report; success is unaffected. CI's one
            # Linux x86 hashlib failure left only "Type: message".
            PCC_DEBUG_WORKER_RERAISE="1",
        )
        command = [str(compiler), "--backend", backend,
                   "--python-libpython", libpython_mode, "--ir-scaffold", ir_scaffold_mode,
                   str(source), "-o", str(output)]
        # stderr streams into its file, so a gate killed mid-compile keeps
        # the coordinator and worker timing printed so far. The 400 s compile
        # budget matches the prior-worker gate's NATIVE_GATE_TIMEOUT_S.
        result = run_process_group_timeout(
            command, env=env, timeout=400,
            stderr_path=str(output) + ".compile.stderr",
        )
        Path(str(output) + ".compile.stdout").write_text(result.stdout, encoding="utf-8")
        assert result.returncode == 0, (
            f"native compiler {compiler} exited {result.returncode}:\n"
            + result.stdout + result.stderr
        )
        assert Path(output).is_file(), f"native compiler {compiler} produced no executable"

    return compile_program
