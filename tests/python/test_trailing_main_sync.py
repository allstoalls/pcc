"""A trailing main() uses ordinary call semantics before choosing its exit code.

The IR cases compile actual source without a runtime build. Integration cases
require an explicit prebuilt threaded archive; host and native compiler owners
are separate nodes. No explicit vthread.run/spawn is used to execute main.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_CASES = {
    "plain_none": ('''def main() -> None:
    print("ordinary-none")
main()
''', 0, "ordinary-none\n", "", False, False),
    "plain_int": ('''def main() -> int:
    print("ordinary-int")
    return 7
main()
''', 7, "ordinary-int\n", "", False, False),
    "park_none": ('''from threading import Event
def main() -> None:
    gate = Event()
    gate.set()
    gate.wait()
    print("parked-main-none")
main()
''', 0, "parked-main-none\n", "", True, False),
    "park_int": ('''from threading import Event
def main() -> int:
    gate = Event()
    gate.set()
    gate.wait()
    print("parked-main-int")
    return 23
main()
''', 23, "parked-main-int\n", "", True, False),
    "park_nested": ('''from threading import Event
def leaf() -> int:
    gate = Event()
    gate.set()
    gate.wait()
    print("leaf")
    return 17
def main() -> int:
    print("main")
    return leaf()
main()
''', 17, "main\nleaf\n", "", True, False),
    "park_exception": ('''from threading import Event
def main() -> None:
    gate = Event()
    gate.set()
    gate.wait()
    print("entered-before-error")
    raise ValueError("trailing-main-error")
main()
''', 1, "entered-before-error\n", "trailing-main-error", True, False),
    "source_generator": ('''from threading import Event
def main():
    gate = Event()
    gate.set()
    gate.wait()
    raise AssertionError("source generator was implicitly driven")
    yield 1
print("generator-created-only")
main()
''', 0, "generator-created-only\n", "", False, True),
    "source_async": ('''from threading import Event
async def main():
    gate = Event()
    gate.set()
    gate.wait()
    raise AssertionError("async function was implicitly driven")
print("coroutine-created-only")
main()
''', 0, "coroutine-created-only\n", "", False, True),
}


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def _source(tmp_path, case, *, runtime_gc=None):
    path = tmp_path / "trailing.py"
    prefix = ""
    if runtime_gc is not None:
        prefix = ("from pcc.extern import extern, c_int64\n"
                  "_probe_threads = extern('pcc_threads_enabled', (), c_int64)\n"
                  "_probe_backend = extern('pcc_gc_backend', (), c_int64)\n"
                  "assert _probe_threads() == 1\n"
                  + "assert _probe_backend() == " + str(runtime_gc) + "\n")
    path.write_text(prefix + _CASES[case][0], encoding="utf-8")
    return path


def _environment(monkeypatch, tmp_path, *, archive=None):
    for name in tuple(os.environ):
        if name.startswith(("PCC_", "DYLD_")) or name == "LC_ALL":
            monkeypatch.delenv(name)
    settings = {
        "PCC_WITH_THREADS": "1", "PCC_PYTHON_IR_PASSES": "off",
        "PCC_SELF_LINK": "pcc", "PCC_IR_TO_OBJ_EMITTER": "pcc",
        "PCC_SELF_BACKEND_OBJECT_CACHE": "0", "PCC_PY_FRONTEND_IR_CACHE": "0",
        "PCC_RUNTIME_CC": str(tmp_path / "forbidden-host-cc"),
        "PCC_HOST_PYTHON": str(tmp_path / "forbidden-host-python"),
        "PCC_NO_AUTO_PCC1": "1", "PCC_GC_BACKEND": "0",
        "PCC_SOURCE_ROOT": str(_ROOT), "PCC_REPO_ROOT": str(_ROOT),
    }
    if archive is not None:
        settings["PCC_RUNTIME_ARCHIVE"] = str(archive)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    return dict(os.environ)


@pytest.mark.parametrize("case", tuple(_CASES))
def test_trailing_main_real_entry_ir(tmp_path, monkeypatch, case):
    from pcc.frontends.python.pipeline import compile_python

    _environment(monkeypatch, tmp_path)
    source = _source(tmp_path, case)
    output = tmp_path / "trailing.ll"
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    ir_text = output.read_text()
    entry = re.search(r"^define\s+(?:external\s+)?i32\s+@main\([^\n]*\)[^\n]*\{\n(.*?)^\}",
                      ir_text, re.M | re.S)
    assert entry is not None, "actual process entry was not emitted"
    body = entry.group(1)
    sync_calls = re.findall(r"\bcall\b[^\n]*@py_gen_run_may_park_sync\(", body)
    assert len(sync_calls) == (1 if _CASES[case][4] else 0), (case, body)
    if _CASES[case][5]:
        assert not re.search(r"\bcall\b[^\n]*@py_int_to_i64\(", body), body
    elif _CASES[case][4]:
        # Exit unboxing, if this call's source result is boxed, must occur
        # after sync drive and consume that final value, not the Gen factory.
        drive = body.index("@py_gen_run_may_park_sync(")
        unbox = body.find("@py_int_to_i64(")
        assert unbox < 0 or unbox > drive, body


@pytest.fixture
def supplied_threaded_runtime():
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

    requested = os.environ.get("PCC_THREADED_RUNTIME_ARCHIVE", "")
    assert requested, "set explicit prebuilt PCC_THREADED_RUNTIME_ARCHIVE; no automatic build"
    archive = Path(requested).resolve(strict=True)
    manifest = verify_runtime_archive_manifest(archive, runtime_root=_ROOT / "pcc/runtime")
    members = {row["member"] for row in manifest["members"]}
    assert "freestanding_thread_kernel_pthread.o" in members
    assert "freestanding_thread_kernel.o" not in members
    return archive


def _execute(binary, environment, directory, case, gc_backend):
    status, stdout, error = _CASES[case][1:4]
    inputs = {str(binary): _sha(binary)}
    _write_json(directory / "execute-command.json", {
        "command": [str(binary)], "gc_backend": gc_backend, "input_sha256": inputs,
        "expected_exit_code": status, "expected_stdout": stdout,
        "exit_convention": "pcc trailing ordinary main: None->0/int->exit; source generators/async discarded",
    })
    try:
        result = subprocess.run([str(binary)], cwd=directory,
                                env=dict(environment, PCC_GC_BACKEND=str(gc_backend), PATH="/nonexistent"),
                                capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        (directory / "execute.stdout").write_bytes(error.stdout or b"")
        (directory / "execute.stderr").write_bytes(error.stderr or b"")
        raise
    (directory / "execute.stdout").write_text(result.stdout)
    (directory / "execute.stderr").write_text(result.stderr)
    assert _sha(binary) == inputs[str(binary)]
    assert result.returncode == status, (case, gc_backend, result.returncode, result.stdout, result.stderr)
    assert result.stdout == stdout, (case, gc_backend, result.stdout, result.stderr)
    if error:
        assert "ValueError" in result.stderr and error in result.stderr, result.stderr


@pytest.mark.integration
@pytest.mark.parametrize("gc_backend", (1, 4), ids=("gc1", "gc4"))
@pytest.mark.parametrize("case", tuple(_CASES))
def test_host_compiled_trailing_main_executes(tmp_path, monkeypatch, supplied_threaded_runtime,
                                             case, gc_backend):
    from pcc.frontends.python.pipeline import compile_python

    archive = supplied_threaded_runtime
    environment = _environment(monkeypatch, tmp_path, archive=archive)
    source = _source(tmp_path, case, runtime_gc=gc_backend)
    binary = tmp_path / "trailing"
    before = {str(path): _sha(path) for path in (archive, source)}
    _write_json(tmp_path / "compile.json", {
        "producer": "host CPython -> owned self backend", "source": str(source),
        "runtime_archive": str(archive), "source_sha256": before,
        "threads": 1, "scaffold": "on", "python_ir_passes": "off",
    })
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(archive))
    assert {path: _sha(path) for path in before} == before
    _execute(binary, environment, tmp_path, case, gc_backend)


@pytest.mark.integration
@pytest.mark.parametrize("gc_backend", (1, 4), ids=("gc1", "gc4"))
@pytest.mark.parametrize("case", tuple(_CASES))
def test_native_compiled_trailing_main_executes(tmp_path, monkeypatch, supplied_threaded_runtime,
                                               case, gc_backend):
    # Resolve explicit authorization/input before the environment is cleaned.
    requested = os.environ.get("PCC_TRAILING_MAIN_COMPILER", "")
    assert requested, "set PCC_TRAILING_MAIN_COMPILER to the new native pcc1/pcc2"
    compiler = Path(requested).resolve(strict=True)
    with compiler.open("rb") as stream:
        magic = stream.read(4)
    assert magic in (b"\xcf\xfa\xed\xfe", b"\x7fELF") or magic[:2] == b"MZ", "a Python wrapper is not pcc1"
    archive = supplied_threaded_runtime
    environment = _environment(monkeypatch, tmp_path, archive=archive)
    source = _source(tmp_path, case, runtime_gc=gc_backend)
    binary = tmp_path / "trailing"
    before = {str(path): _sha(path) for path in (compiler, archive, source)}
    command = [str(compiler), "--backend", "self", "--python-libpython", "off",
               "--ir-scaffold", "on", str(source), "-o", str(binary)]
    _write_json(tmp_path / "compile.json", {"producer": "explicit native compiler", "command": command,
                                            "input_sha256": before})
    result = subprocess.run(command, cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=120)
    (tmp_path / "compile.stdout").write_text(result.stdout)
    (tmp_path / "compile.stderr").write_text(result.stderr)
    assert result.returncode == 0, (case, result.stdout, result.stderr)
    assert {path: _sha(path) for path in before} == before
    _execute(binary, environment, tmp_path, case, gc_backend)
