"""Native threaded path ownership qualification, with explicit frozen inputs.

Run under the shared performance lock and process-tree RSS watchdog. The suite
never builds a runtime or bootstraps a compiler. CPython supplies expected path
values only; the tested programs are compiled by pcc1 and executed directly.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import pytest


pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests/fixtures/native/threaded_path_ownership"
_GC_BACKENDS = (1, 4)
_NATIVE_MAGICS = (b"\xcf\xfa\xed\xfe", b"\x7fELF")


def _sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _unchanged(identities):
    assert {path: _sha256(path) for path in identities} == identities


def _run(command, *, cwd, environment, label, timeout, identities, execution_cwd=None):
    _unchanged(identities)
    actual_cwd = cwd if execution_cwd is None else execution_cwd
    receipt = {
        "command": [str(item) for item in command], "cwd": str(actual_cwd),
        "artifact_directory": str(cwd),
        "environment": {key: value for key, value in environment.items()
                        if key.startswith("PCC_") or key == "PATH"},
        "input_sha256": identities, "timeout_seconds": timeout,
    }
    record = cwd / (label + ".json")
    _write_json(record, receipt)
    began = time.monotonic()
    try:
        result = subprocess.run(command, cwd=actual_cwd, env=environment,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        (cwd / (label + ".stdout")).write_bytes(error.stdout or b"")
        (cwd / (label + ".stderr")).write_bytes(error.stderr or b"")
        receipt.update(status="timeout", elapsed_seconds=time.monotonic() - began)
        _write_json(record, receipt)
        raise
    else:
        (cwd / (label + ".stdout")).write_bytes(result.stdout)
        (cwd / (label + ".stderr")).write_bytes(result.stderr)
        receipt.update(returncode=result.returncode,
                       elapsed_seconds=time.monotonic() - began)
        _write_json(record, receipt)
        assert result.returncode == 0, (command, result.returncode,
                                        result.stdout, result.stderr)
        return result
    finally:
        _unchanged(identities)


@pytest.fixture(scope="module")
def path_toolchain():
    from tests.runtime_fixture_provenance import _verified_test_runtime_archive

    requested = os.environ.get("PCC_PATH_OWNERSHIP_COMPILER", "")
    assert requested, "set PCC_PATH_OWNERSHIP_COMPILER to the candidate native pcc1"
    compiler = Path(requested).resolve(strict=True)
    assert compiler.is_file() and os.access(compiler, os.X_OK)
    with compiler.open("rb") as stream:
        magic = stream.read(4)
    assert magic in _NATIVE_MAGICS or magic[:2] == b"MZ", "a script is not native pcc1"

    requested = os.environ.get("PCC_THREADED_RUNTIME_ARCHIVE", "")
    assert requested, "set explicit PCC_THREADED_RUNTIME_ARCHIVE; no cached build"
    archive = Path(requested).resolve(strict=True)
    source_root = Path(os.environ.get("PCC_PATH_OWNERSHIP_SOURCE_ROOT", str(_ROOT))).resolve(strict=True)
    runtime_root = source_root / "pcc/runtime"
    archive, manifest = _verified_test_runtime_archive(
        archive, threads=True, runtime_root=runtime_root,
    )
    members = {row["member"]: row for row in manifest["members"]}
    assert "freestanding_thread_kernel_pthread.o" in members, "requires real threaded runtime"
    assert "freestanding_thread_kernel.o" not in members, "stub thread kernel is not qualified"
    for row in members.values():
        assert row["object_emitter"] == "pcc-self-backend-object-writer", row["member"]
    identities = {
        str(path): _sha256(path) for path in (
            compiler, archive, Path(str(archive) + ".provenance.json"),
            Path(str(archive) + ".capi_syms"),
            source_root / "pcc/frontends/python/codegen/native_os.py",
            runtime_root / "include/py_runtime.h",
            runtime_root / "py/freestanding_gc_root_operations.py",
            runtime_root / "py/py_obj.py", runtime_root / "py/py_os_path.py",
        )
    }
    yield compiler, archive, source_root, members, identities
    _unchanged(identities)


def _environment(toolchain, directory, gc_backend):
    _compiler, archive, source_root, members, _identities = toolchain
    # Admission has already required one complete, coherent configuration.
    # Preserve it when clearing the caller's PCC_* routing/environment.
    refcount = next(iter(members.values()))["runtime_build_config"]["refcount"]
    # Eliminate replay plans, alternate runtime lookup and sampler injection.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PCC_", "DYLD_", "PYTHON")) and key != "LC_ALL"}
    environment.update(
        PCC_SOURCE_ROOT=str(source_root), PCC_REPO_ROOT=str(source_root),
        PCC_RUNTIME_DIR=str(source_root / "pcc/runtime"),
        PCC_RUNTIME_ARCHIVE=str(archive), PCC_WITH_THREADS="1", PCC_RUNTIME_HIGH="py",
        PCC_REFCOUNT_KIND=refcount,
        PCC_SELF_LINK="pcc", PCC_SELF_OBJ="pcc", PCC_IR_TO_OBJ_EMITTER="pcc",
        PCC_PYTHON_IR_PASSES="off", PCC_GC_BACKEND=str(gc_backend),
        PCC_NO_AUTO_PCC1="1", PCC_SELF_BACKEND_OBJECT_CACHE="off",
        PCC_HOST_PYTHON=str(directory / "forbidden-python"),
        PCC_RUNTIME_CC=str(directory / "forbidden-cc"),
        CC=str(directory / "forbidden-cc"),
    )
    return environment


def _oracle(cwd, *, normalized):
    # All cases are ordinary str. No PathLike/bytes scope is smuggled in here.
    long_component = "前缀🙂" * 96
    if normalized:
        absolute = ["", "plain", ".", "a/../b", "./a/./b", "a//b///c", "/a/../b", "../../outside",
                    "//server/share/a", "///server/share/a", "/../../x", "//../../x"]
        relative = [("a/../b", "."), ("./a//b", "a"), ("a/b/../c", "a/d/.."),
                    ("重复/../重复/子", "重复/./前"), ("a/../b", ""),
                    ("//server/share/a", "//server/share"),
                    ("///server/share/a", "/server/share"), ("/../../x", "/")]
    else:
        absolute = ["", "plain", "unicode-中文/子级🙂", long_component + "/尾部", "/absolute/中文"]
        relative = [("one/two", "one"), ("one", "one/two"), ("one/two", "one/two"),
                    ("前缀/前缀/甲", "前缀/前缀/乙"),
                    (long_component + "/same/tail", long_component + "/same/start"),
                    ("前缀相似/甲", "前缀/乙"), ("plain", "")]
    # Use stdlib abspath/relpath under the exact native execution cwd. Parent
    # cwd is restored before starting a subprocess and is never changed by it.
    previous = Path.cwd()
    try:
        os.chdir(cwd)
        abs_cases = [(path, os.path.abspath(path)) for path in absolute]
        norm_cases = [(path, os.path.normpath(path)) for path in absolute]
        rel_cases = [(path, start, os.path.relpath(path, start)) for path, start in relative]
        try:
            os.path.relpath("")
        except ValueError as error:
            empty_exception = type(error).__name__
        else:
            raise AssertionError("host stdlib did not reject empty relpath")
        for start in ("", "plain"):
            try:
                os.path.relpath("", start)
            except ValueError:
                pass
            else:
                raise AssertionError("host stdlib did not reject explicit empty relpath")
    finally:
        os.chdir(previous)
    return {"cwd": str(cwd), "abspath": abs_cases, "normpath": norm_cases, "relpath": rel_cases,
            "empty_relpath_exception": empty_exception}


def _program(toolchain, directory, fixture_name, gc_backend, *, normalized=False, run_cwd=None):
    compiler, _archive, source_root, _members, identities = toolchain
    oracle = _oracle(directory if run_cwd is None else run_cwd, normalized=normalized)
    _write_json(directory / "cpython-stdlib-oracle.json", oracle)
    template = (_FIXTURES / fixture_name).read_text(encoding="utf-8")
    if fixture_name.endswith(".c"):
        rendered = template.replace("EXPECTED_BACKEND", str(gc_backend))
    else:
        rendered = template.replace("__ABS_CASES__", repr(oracle["abspath"]))
        rendered = rendered.replace("__NORM_CASES__", repr(oracle["normpath"]))
        rendered = rendered.replace("__REL_CASES__", repr(oracle["relpath"]))
        rendered = rendered.replace("__EXPECTED_BACKEND__", str(gc_backend))
    source = directory / fixture_name
    source.write_text(rendered, encoding="utf-8")
    executable = directory / ("probe.exe" if os.name == "nt" else "probe")
    environment = _environment(toolchain, directory, gc_backend)
    inputs = dict(identities, **{str(source): _sha256(source)})
    command = [str(compiler), "--backend", "self"]
    if source.suffix == ".c":
        command.extend(["--freestanding-libc", "--cpp-arg=-I" + str(source_root / "pcc/runtime/include")])
    else:
        command.extend(["--python-libpython", "off", "--ir-scaffold", "on"])
    command.extend([str(source), "-o", str(executable)])
    _run(command, cwd=directory, environment=environment, label="compile", timeout=180,
         identities=inputs)
    assert executable.is_file()
    inputs[str(executable)] = _sha256(executable)
    _write_json(directory / "identities.json", inputs)
    return executable, environment, inputs


@pytest.mark.parametrize("gc_backend", _GC_BACKENDS, ids=("gc1", "gc4"))
def test_native_threaded_path_values_and_operand_lifetimes(path_toolchain, tmp_path, gc_backend):
    executable, environment, identities = _program(path_toolchain, tmp_path, "values.py", gc_backend)
    result = _run([str(executable)], cwd=tmp_path, environment=dict(environment, PATH="/nonexistent"),
                  label="execute", timeout=45, identities=identities)
    assert result.stdout == b"path-values-and-lifetimes-ok\n", result.stdout


@pytest.mark.parametrize("gc_backend", _GC_BACKENDS, ids=("gc1", "gc4"))
def test_native_threaded_path_dot_components_match_stdlib(path_toolchain, tmp_path, gc_backend):
    # A separate real gate, not xfail: pre-existing abspath normalization
    # differences must not redefine the oracle or conceal ownership evidence.
    executable, environment, identities = _program(path_toolchain, tmp_path, "values.py", gc_backend,
                                                   normalized=True)
    result = _run([str(executable)], cwd=tmp_path, environment=dict(environment, PATH="/nonexistent"),
                  label="execute", timeout=45, identities=identities)
    assert result.stdout == b"path-values-and-lifetimes-ok\n", result.stdout


@pytest.mark.parametrize("gc_backend", _GC_BACKENDS, ids=("gc1", "gc4"))
def test_native_threaded_abspath_from_root_cwd(path_toolchain, tmp_path, gc_backend):
    assert os.name == "posix", "this node qualifies POSIX root-cwd joining"
    # Compile, source, executable and logs stay under tmp_path. The program
    # only computes paths; cwd='/' causes no file creation in the real root.
    executable, environment, identities = _program(
        path_toolchain, tmp_path, "values.py", gc_backend,
        normalized=True, run_cwd=Path("/"),
    )
    oracle = json.loads((tmp_path / "cpython-stdlib-oracle.json").read_text())
    assert ("plain", "/plain") in [tuple(case) for case in oracle["abspath"]]
    result = _run(
        [str(executable)], cwd=tmp_path, execution_cwd=Path("/"),
        environment=dict(environment, PATH="/nonexistent"), label="execute",
        timeout=45, identities=identities,
    )
    assert result.stdout == b"path-values-and-lifetimes-ok\n", result.stdout


@pytest.mark.parametrize("gc_backend", _GC_BACKENDS, ids=("gc1", "gc4"))
def test_native_threaded_paths_survive_background_collector(path_toolchain, tmp_path, gc_backend):
    executable, environment, identities = _program(path_toolchain, tmp_path, "concurrent.py", gc_backend)
    result = _run([str(executable)], cwd=tmp_path, environment=dict(environment, PATH="/nonexistent"),
                  label="execute", timeout=60, identities=identities)
    assert result.stdout == b"threaded-path-collector-ok\n", result.stdout


@pytest.mark.parametrize("gc_backend", _GC_BACKENDS, ids=("gc1", "gc4"))
def test_native_pin_public_abi_flags_and_metric_balance(path_toolchain, tmp_path, gc_backend):
    executable, environment, identities = _program(path_toolchain, tmp_path, "pin_abi.c", gc_backend)
    result = _run([str(executable)], cwd=tmp_path, environment=dict(environment, PATH="/nonexistent"),
                  label="execute", timeout=15, identities=identities)
    assert result.stdout == b"pin-public-abi-and-metric-ok\n", result.stdout


def test_supplied_archive_pin_unique_owner_and_actual_leaf_ir(path_toolchain, tmp_path):
    from pcc.backend.ar import read_members
    from pcc.backend.ar_writer import _defined_symbols

    _compiler, archive, _source_root, records, identities = path_toolchain
    requested = os.environ.get("PCC_PATH_OWNERSHIP_IR_DIR", "")
    assert requested, "set PCC_PATH_OWNERSHIP_IR_DIR to retained IR used for this archive"
    ir_directory = Path(requested).resolve(strict=True)
    paths = {name: ir_directory / (name + ".ll")
             for name in ("freestanding_gc_root_operations", "py_obj", "py_os_path")}
    for name, path in paths.items():
        assert _sha256(path) == records[name + ".o"]["ir_sha256"], name
    owner_ir = paths["freestanding_gc_root_operations"].read_text()
    managed_ir = paths["py_obj"].read_text()
    definitions = {"pcc_gc_pin": [], "pcc_gc_unpin": [], "pcc_gc_take_pinned_slot": []}
    for member, payload in read_members(archive.read_bytes()):
        kind, names = _defined_symbols(payload)
        for name in names:
            normalized = name[1:] if kind == "macho" and name.startswith("_") else name
            if normalized in definitions:
                definitions[normalized].append(member)
    for symbol in definitions:
        assert definitions[symbol] == ["freestanding_gc_root_operations.o"], definitions
    for symbol in ("pcc_gc_pin", "pcc_gc_unpin"):
        pattern = r"^define\s+(?:external\s+)?void\s+@" + symbol + r"\(([^\n]*)\)[^\n]*\{\n(.*?)^\}"
        functions = list(re.finditer(pattern, owner_ir, re.M | re.S))
        assert len(functions) == 1, symbol
        args, body = functions[0].groups()
        assert re.fullmatch(r"(?:i8\*|ptr)\s+[^,]+", args), (symbol, args)
        assert not re.search(r"\b(?:call|invoke|callbr)\b", body), (symbol, body)
        assert "load i32" in body and "store i32" in body, symbol
        assert "@pcc_gc_metric_pin" in body, symbol
        assert not re.search(r"^define\b[^\n]*@" + symbol + r"\(", managed_ir, re.M)
        assert re.search(r"^declare\s+(?:external\s+)?void\s+@" + symbol + r"\(", managed_ir, re.M), symbol
    transfer = list(re.finditer(
        r"^define\s+(?:external\s+)?(?:i8\*|ptr)\s+@pcc_gc_take_pinned_slot\(([^\n]*)\)[^\n]*\{\n(.*?)^\}",
        owner_ir, re.M | re.S,
    ))
    assert len(transfer) == 1, "missing unique pointer-return transfer ABI"
    args, body = transfer[0].groups()
    assert re.fullmatch(r"(?:i8\*|ptr)\s+[^,]+,\s*i64\s+[^,]+", args), args
    # Depending on the owned inline pass, the strict unpin can remain a direct
    # call or disappear. No indirect/other call can enter this return handoff.
    calls = [line for line in body.splitlines() if re.search(r"\b(?:call|invoke|callbr)\b", line)]
    for line in calls:
        assert re.search(r"\bcall\s+void(?:\s+\([^)]*\))?\s+@pcc_gc_unpin\(", line), line
    assert re.search(r"store\s+(?:i8\*|ptr)\s+null\b", body), body
    path_ir = paths["py_os_path"].read_text()
    for symbol in ("py_os_path_abspath", "py_os_path_normpath", "py_os_path_relpath"):
        wrappers = list(re.finditer(
            r"^define\s+(?:external\s+)?(?:i8\*|ptr)\s+@" + symbol
            + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", path_ir, re.M | re.S,
        ))
        assert len(wrappers) == 1, symbol
        lines = [line.strip() for line in wrappers[0].group(1).splitlines() if line.strip()]
        transfers = [index for index, line in enumerate(lines)
                     if re.search(r"\bcall\b.*@pcc_gc_take_pinned_slot\(", line)]
        assert len(transfers) == 1, (symbol, transfers)
        transfer_index = transfers[0]
        # No implicit return cleanup, frame leave, poll or managed temporary
        # operation is allowed after the strict ownership transfer.
        result_name = lines[transfer_index].split("=", 1)[0].strip()
        # Other CFG blocks can be serialized after this return block. Check
        # the next instruction in the transfer block, not end-of-function text.
        assert lines[transfer_index + 1] in (
            "ret ptr " + result_name, "ret i8* " + result_name,
        ), (symbol, lines[transfer_index:transfer_index + 3])
        frame_leaves = [index for index, line in enumerate(lines)
                        if re.search(r"\bcall\b.*@pcc_gc_frame_leave\(", line)]
        assert frame_leaves and max(frame_leaves) < transfer_index, symbol
    _write_json(tmp_path / "bound-pin-ir-and-symbols.json", {
        "objects": definitions, "input_sha256": identities,
        "ir_sha256": {str(path): _sha256(path) for path in paths.values()},
        "proof_scope": "exact archive member ownership plus its receipt-bound emitted leaf IR",
    })
    _unchanged(identities)
