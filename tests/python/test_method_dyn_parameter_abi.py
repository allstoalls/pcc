"""Unannotated method formals keep their object ABI despite local scalar calls."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = '''class Selector:
    def choose(self, *, level=True):
        if isinstance(level, bool):
            return 10 if level else 20
        assert isinstance(level, int)
        return 100 + level

    def internal(self):
        return self.choose(level=False)
'''
CHECK = '''
def main():
    value = Selector()
    assert value.internal() == 20
    assert value.choose() == 10
    assert value.choose(level=False) == 20
    assert value.choose(level=True) == 10
    assert value.choose(level=0) == 100
    assert value.choose(level=2) == 102
    large = 2 ** 80
    assert value.choose(level=large) == large + 100
    print("method-dyn-abi-ok")
main()
'''
OBJECT_HINT = '''class Core:
    scratch: list[int]
    def __init__(self, scratch: list[int]):
        self.scratch = scratch
class Machine:
    def run(self, core: Core):
        self.step(core, 1)
    def step(self, core, index):
        print(core.scratch[index])
def main():
    Machine().run(Core([10, 42]))
main()
'''


def _configure(monkeypatch, runtime=None):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1")
    monkeypatch.setenv("PCC_SELF_BACKEND_OBJECT_CACHE", "0")
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE", "0")
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_NO_AUTO_PCC1", "1")
    monkeypatch.delenv("PCC_TEST_COMPILER", raising=False)
    if runtime is not None:
        monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(runtime))


def _compile(tmp_path, split, scaffold, *, ir_only, runtime=None):
    from pcc.frontends.python.pipeline import compile_python, compile_python_multi

    output = tmp_path / ("method.ll" if ir_only else "method")
    entry = tmp_path / "method_entry.py"
    options = dict(backend="self", libpython_mode="off", ir_scaffold_mode=scaffold,
                   emit_llvm_only=ir_only)
    if runtime is not None:
        options["runtime_archive"] = str(runtime)
    if split:
        provider = tmp_path / "method_provider.py"
        provider.write_text(PROVIDER)
        entry.write_text("from method_provider import Selector\n" + CHECK)
        compile_python_multi([str(provider), str(entry)], str(output),
                             module_names=["method_provider", "method_entry"],
                             entry_module="method_entry", **options)
    else:
        entry.write_text(PROVIDER + CHECK)
        compile_python(str(entry), str(output), **options)
    return output


@pytest.mark.parametrize("split", (False, True), ids=("local", "cross-module"))
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_unannotated_method_real_ir_keeps_pointer_formal(tmp_path, monkeypatch, split, scaffold):
    _configure(monkeypatch)
    output = _compile(tmp_path, split, scaffold, ir_only=True)
    text = output.read_text()
    declarations = list(re.finditer(r"^define[^\n]*@[^\n(]*_Selector_choose\(([^\n]*)\)[^\n]*\{", text, re.M))
    assert len(declarations) == 1, text
    parameters = declarations[0].group(1).split(",")
    assert len(parameters) == 2
    assert re.match(r"\s*(?:ptr|i8\*)\s", parameters[1]), parameters
    # A narrowed body would constant-fold the bool check even if an extern
    # declaration were patched. The generic object predicate must remain.
    body = text[declarations[0].end():].split("\n}", 1)[0]
    assert "@py_obj_type_tag(" in body or "@py_obj_isinstance(" in body, body


def test_pointer_object_shape_hint_survives_abi_guard(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    _configure(monkeypatch)
    source = tmp_path / "object_hint.py"
    source.write_text(OBJECT_HINT)
    output = tmp_path / "object_hint.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, ir_scaffold_mode="on")
    text = output.read_text()
    method = re.search(r"^define[^\n]*@[^\n(]*_Machine_step\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert method is not None
    body = method.group(1)
    assert "@py_instance_get_field(" in body
    assert re.search(r"\bcall\b[^\n]*@py_list_get(?:item)?\(", body)
    assert "@py_obj_getattr(" not in body


@pytest.fixture
def explicit_runtime():
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

    requested = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert requested, "set an explicit prebuilt matching runtime; no automatic build"
    archive = Path(requested).resolve(strict=True)
    verify_runtime_archive_manifest(archive, runtime_root=_ROOT / "pcc/runtime")
    return archive


@pytest.mark.integration
@pytest.mark.parametrize("split", (False, True), ids=("local", "cross-module"))
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_host_emitted_method_preserves_int_value_and_type(tmp_path, monkeypatch, explicit_runtime, split, scaffold):
    _configure(monkeypatch, explicit_runtime)
    binary = _compile(tmp_path, split, scaffold, ir_only=False, runtime=explicit_runtime)
    for backend in (1, 4):
        result = subprocess.run([str(binary)], cwd=tmp_path, capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"))
        (tmp_path / ("execute-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("execute-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == "method-dyn-abi-ok\n", (backend, result.stdout, result.stderr)
