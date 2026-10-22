"""Module aliases retain their exported class identity despite leaf collisions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_C_ENUM = "enum { VALUE = 3 };\nint main(void) { return VALUE == 3 ? 0 : 1; }\n"


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _environment(monkeypatch, tmp_path, *, runtime=None):
    for key in tuple(os.environ):
        if key.startswith(("PCC_", "DYLD_")) or key == "LC_ALL":
            monkeypatch.delenv(key)
    values = {
        "PCC_NO_AUTO_PCC1": "1", "PCC_PYTHON_IR_PASSES": "off",
        "PCC_PY_FRONTEND_JOBS": "1", "PCC_SELF_BACKEND_JOBS": "1",
        "PCC_PY_FRONTEND_IR_CACHE": "0", "PCC_SELF_BACKEND_OBJECT_CACHE": "0",
        "PCC_SELF_LINK": "pcc", "PCC_IR_TO_OBJ_EMITTER": "pcc",
        "PCC_SOURCE_ROOT": str(_ROOT), "PCC_REPO_ROOT": str(_ROOT),
        "PCC_RUNTIME_CC": str(tmp_path / "forbidden-host-cc"),
        "PCC_HOST_PYTHON": str(tmp_path / "forbidden-host-python"),
        "PCC_WITH_THREADS": "1", "PCC_GC_BACKEND": "0",
    }
    if runtime is not None:
        values["PCC_RUNTIME_ARCHIVE"] = str(runtime)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return dict(os.environ)


def _modules(tmp_path, reverse):
    providers = []
    for name, tag in (("owner_left", 11), ("owner_right", 22)):
        path = tmp_path / (name + ".py")
        path.write_text("class Token:\n    def __init__(self):\n        self.tag = " + str(tag) + "\n")
        providers.append((name, path))
    if reverse:
        providers.reverse()
    imports = ["import owner_left as first", "import owner_right as second"]
    if reverse:
        imports.reverse()
    entry = tmp_path / "owner_main.py"
    entry.write_text("\n".join(imports) + '''
def first_match(value):
    return isinstance(value, first.Token)

def second_match(value):
    return isinstance(value, second.Token)

def main():
    left = first.Token()
    right = second.Token()
    assert left.tag == 11 and right.tag == 22
    assert first_match(left) and not first_match(right)
    assert second_match(right) and not second_match(left)
    assert not isinstance(left, (second.Token, int))
    assert not isinstance(right, (first.Token, int))
    assert isinstance(left, (first.Token, second.Token))
    assert isinstance(right, (first.Token, second.Token))
    print("qualified-owner-ok")
main()
''')
    return [str(path) for _name, path in providers] + [str(entry)], [name for name, _path in providers] + ["owner_main"]


def _function(ir_text, name):
    matches = list(re.finditer(r"^define[^\n]*@(?:[A-Za-z0-9_.]+_)?" + name
                              + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", ir_text, re.M | re.S))
    assert len(matches) == 1, (name, len(matches))
    return matches[0].group(1)


@pytest.mark.parametrize("reverse", (False, True), ids=("left-first", "right-first"))
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_alias_classinfo_real_ir_keeps_owning_module(tmp_path, monkeypatch, reverse, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi

    _environment(monkeypatch, tmp_path)
    paths, names = _modules(tmp_path, reverse)
    output = tmp_path / "owner.ll"
    compile_python_multi(paths, str(output), module_names=names, entry_module="owner_main",
                         emit_llvm_only=True, backend="self", libpython_mode="off",
                         ir_scaffold_mode=scaffold)
    text = output.read_text()
    for function, owner, other in (("first_match", "owner_left", "owner_right"),
                                   ("second_match", "owner_right", "owner_left")):
        body = _function(text, function)
        assert "@.class." + owner + ".Token" in body, body
        assert "@.class." + other + ".Token" not in body, body
        assert re.search(r"\bcall\b[^\n]*@py_isinstance\(", body), body


@pytest.fixture
def explicit_runtime():
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

    requested = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert requested, "set explicit matching PCC_RUNTIME_ARCHIVE; no implicit runtime build"
    archive = Path(requested).resolve(strict=True)
    verify_runtime_archive_manifest(archive, runtime_root=_ROOT / "pcc/runtime")
    return archive


@pytest.mark.integration
@pytest.mark.parametrize("reverse", (False, True), ids=("left-first", "right-first"))
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_host_compiled_module_alias_checks_execute(tmp_path, monkeypatch, explicit_runtime, reverse, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi

    environment = _environment(monkeypatch, tmp_path, runtime=explicit_runtime)
    paths, names = _modules(tmp_path, reverse)
    output = tmp_path / "owner"
    before = {path: _sha(path) for path in paths + [str(explicit_runtime)]}
    compile_python_multi(paths, str(output), module_names=names, entry_module="owner_main",
                         backend="self", libpython_mode="off", ir_scaffold_mode=scaffold,
                         runtime_archive=str(explicit_runtime))
    for backend in (1, 4):
        result = subprocess.run([str(output)], cwd=tmp_path,
                                env=dict(environment, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"),
                                text=True, capture_output=True, timeout=15)
        (tmp_path / ("execute-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("execute-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == "qualified-owner-ok\n", (backend, result.stdout, result.stderr)
    assert {path: _sha(path) for path in before} == before
    (tmp_path / "identities.json").write_text(json.dumps(dict(before, **{str(output): _sha(output)}), indent=2) + "\n")


def _c_probe(tmp_path, monkeypatch, compiler, *, native):
    environment = _environment(monkeypatch, tmp_path)
    source = tmp_path / "enum.c"
    source.write_text(_C_ENUM)
    ir = tmp_path / "enum.ll"
    binary = tmp_path / "enum"
    prefix = [str(compiler)] if native else [str(compiler), "-P", "-m", "pcc"]
    # -O0 deliberately isolates this class-identity gate from the separately
    # recorded native default-owned-pass-list failure. No external backend.
    commands = [prefix + ["--backend", "self", "-O0", "--emit-llvm=" + str(ir), str(source)],
                prefix + ["--backend", "self", "-O0", str(source), "-o", str(binary)]]
    (tmp_path / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    for index, command in enumerate(commands):
        result = subprocess.run(command, cwd=_ROOT, env=environment,
                                capture_output=True, text=True, timeout=90)
        (tmp_path / ("compile-" + str(index) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("compile-" + str(index) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, (command, result.stdout, result.stderr)
    assert "define" in ir.read_text() and "@main(" in ir.read_text()
    result = subprocess.run([str(binary)], cwd=tmp_path, env=dict(environment, PATH="/nonexistent"),
                            capture_output=True, text=True, timeout=10)
    (tmp_path / "execute.stdout").write_text(result.stdout)
    (tmp_path / "execute.stderr").write_text(result.stderr)
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.integration
def test_host_owned_minimal_c_enum_ir_and_execution(tmp_path, monkeypatch):
    _c_probe(tmp_path, monkeypatch, sys.executable, native=False)


@pytest.mark.integration
def test_new_native_compiler_minimal_c_enum_ir_and_execution(tmp_path, monkeypatch):
    requested = os.environ.get("PCC_CLASS_OWNER_COMPILER", "")
    assert requested, "set PCC_CLASS_OWNER_COMPILER to rebuilt native pcc1; no host substitution"
    compiler = Path(requested).resolve(strict=True)
    with compiler.open("rb") as stream:
        magic = stream.read(4)
    assert magic in (b"\xcf\xfa\xed\xfe", b"\x7fELF") or magic[:2] == b"MZ"
    _c_probe(tmp_path, monkeypatch, compiler, native=True)
