"""Pointer module attributes have one canonical value across native consumers."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest

_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = """VALUES = ("initial",)
ITEMS = []
NUMBER = 7
BIG = 1000000000000000000000000000000
TEXT = "initial"
TOKEN = None

def read_token():
    return TOKEN

def read_values():
    return VALUES

def read_number():
    return NUMBER

def change_number(value):
    global NUMBER
    NUMBER = value
"""
OBSERVER = """import storage_provider as provider

def read_token():
    return provider.TOKEN

def read_values():
    return provider.VALUES

def read_number():
    return provider.NUMBER

def read_big():
    return provider.BIG

def fresh_token():
    from storage_provider import TOKEN
    return TOKEN
"""
ENTRY = """import storage_provider as provider
import storage_observer as observer
import gc

def main():
    borrowed = ["new"]
    provider.TOKEN = borrowed
    assert provider.TOKEN is borrowed
    assert provider.read_token() is borrowed
    assert observer.read_token() is borrowed
    assert observer.fresh_token() is borrowed
    provider.TOKEN = provider.TOKEN
    gc.collect()
    assert provider.read_token() is borrowed
    assert observer.read_token() is borrowed
    values = ("replacement",)
    provider.VALUES = values
    assert provider.read_values() is values
    assert observer.read_values() is values
    from storage_provider import VALUES
    assert VALUES is values
    items = ["item"]
    provider.ITEMS = items
    from storage_provider import ITEMS
    assert ITEMS is items
    provider.NUMBER = 11
    assert provider.read_number() == 11
    assert observer.read_number() == 11
    from storage_provider import NUMBER
    assert NUMBER == 11
    provider.change_number(19)
    assert provider.NUMBER == 19
    assert observer.read_number() == 19
    # The entry imports pcc.extern and therefore uses raw integer ABI. Its
    # source-backed attribute read must preserve the provider's actual object.
    large = observer.read_big()
    assert provider.BIG is large
    assert large == 1000000000000000000000000000000
    provider.NUMBER = large
    assert provider.NUMBER is large
    assert observer.read_number() is large
    assert provider.read_number() is large
    from storage_provider import NUMBER as large_import
    assert large_import is large
    text = "replacement text"
    provider.TEXT = text
    from storage_provider import TEXT
    assert TEXT is text
    assert provider.TEXT is text
    provider.TOKEN = None
    gc.collect()
    assert borrowed == ["new"]
    print("module-pointer-storage-ok")
main()
"""
LIFETIME = """import storage_provider as provider
import storage_observer as observer
import gc
import weakref
replacement = None
events = []

class Tracked:
    def __del__(self):
        assert provider.TOKEN is replacement
        assert provider.read_token() is replacement
        assert observer.read_token() is replacement
        assert observer.fresh_token() is replacement
        events.append("released")
        gc.collect()

def install_old():
    old = Tracked()
    reference = weakref.ref(old)
    provider.TOKEN = old
    return reference

def main():
    global replacement
    reference = install_old()
    replacement = ["published"]
    provider.TOKEN = replacement
    gc.collect()
    assert events == ["released"]
    assert reference() is None
    assert observer.fresh_token() is replacement
    print("module-pointer-lifetime-ok")
main()
"""


def _configure(monkeypatch, runtime=None):
    for name, value in {
        "PCC_PYTHON_IR_PASSES": "off", "PCC_PY_FRONTEND_JOBS": "1",
        "PCC_SELF_BACKEND_JOBS": "1", "PCC_WITH_THREADS": "1",
        "PCC_NO_AUTO_PCC1": "1", "PCC_SELF_LINK": "pcc",
        "PCC_SELF_BACKEND_OBJECT_CACHE": "0", "PCC_PY_FRONTEND_IR_CACHE": "0",
    }.items():
        monkeypatch.setenv(name, value)
    if runtime is not None:
        monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(runtime))


def _sources(tmp_path, lifetime=False):
    files = []
    prefix = ("from pcc.extern import extern, c_int64\nimport sys\n"
              "_threads = extern('pcc_threads_enabled', (), c_int64)\n"
              "_backend = extern('pcc_gc_backend', (), c_int64)\n"
              "assert _threads() == 1\nassert _backend() == int(sys.argv[1])\n")
    for name, source in (("storage_provider", PROVIDER), ("storage_observer", OBSERVER),
                         ("storage_entry", prefix + (LIFETIME if lifetime else ENTRY))):
        path = tmp_path / (name + ".py")
        path.write_text(source)
        files.append(str(path))
    return files


@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_pointer_attribute_real_ir_uses_provider_cell(tmp_path, monkeypatch, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    output = tmp_path / "module-storage.ll"
    compile_python_multi(_sources(tmp_path), str(output),
                         module_names=["storage_provider", "storage_observer", "storage_entry"],
                         entry_module="storage_entry", backend="self", libpython_mode="off",
                         ir_scaffold_mode=scaffold, emit_llvm_only=True)
    text = output.read_text()
    assert "module.attr.publish" in text
    assert "module.attr.new.take" in text and "module.attr.old.take" in text
    assert "@pcc_gc_take_pinned_slot(" in text
    observer = re.search(r"^define[^\n]*@[^\n(]*storage_observer_read_number\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert observer is not None
    assert ".modvar.storage_provider.NUMBER" in observer.group(1), observer.group(1)
    assert "strict.nolib.stub" not in text


@pytest.mark.parametrize("provider_name,boxed", (("storage_provider", True), ("pcc.storage_provider", False)))
def test_source_constant_storage_survives_export_wire(tmp_path, provider_name, boxed):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.pipeline_exports import _write_native_exports_wire, _read_native_exports_wire
    source = tmp_path / "provider.py"
    source.write_text("NUMBER = 7\nBIG = 1" + "0" * 30 + "\nTEXT = 'x'\nTOKEN = None\nFLAG = True\n")
    _, exports, derived = build_closed_world_context([str(source)], [provider_name], merge_exports=False)
    path = tmp_path / "exports.json"
    _write_native_exports_wire(str(path), exports, derived)
    restored, _ = _read_native_exports_wire(str(path))
    assert restored == exports
    from pcc.frontends.python.pipeline_exports import _read_native_exports_wire_for_module
    from pcc.frontends.python.type_infer import build_unique_external_class_preload_index
    indexed_path = tmp_path / "exports.indexed"
    _write_native_exports_wire(str(indexed_path), exports, derived,
                               module_dependencies={provider_name: ()},
                               unique_class_preload_index=build_unique_external_class_preload_index(exports))
    indexed, _, _, is_indexed = _read_native_exports_wire_for_module(str(indexed_path), provider_name)
    assert is_indexed and indexed == exports
    values = restored[provider_name]
    assert values["NUMBER"]["has_module_storage"] is True
    assert values["NUMBER"]["value_ty"][0] == "int"
    assert values["NUMBER"]["box_int_abi"] is boxed
    assert values["BIG"]["box_int_abi"] is True
    assert values["NUMBER"]["storage_owner"] == ("managed" if boxed else "unknown")
    assert values["BIG"]["storage_owner"] == "managed"
    assert values["TEXT"]["storage_owner"] == "managed"
    assert values["TOKEN"]["storage_owner"] == "managed"
    assert all(values[key]["has_module_storage"] for key in ("TEXT", "TOKEN", "FLAG"))


@pytest.mark.parametrize("declaration", ("FLAG = True", "FLAG = 1.5", "from pcc.extern import c_int64\nFLAG = 7"))
def test_raw_module_attribute_mutation_fails_explicitly(tmp_path, monkeypatch, capfd, declaration):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    provider = tmp_path / "raw_provider.py"
    provider.write_text(declaration + "\n")
    entry = tmp_path / "entry.py"
    entry.write_text("import raw_provider\ndef main():\n    raw_provider.FLAG = []\nmain()\n")
    with pytest.raises(Exception) as caught:
        compile_python_multi([str(provider), str(entry)], str(tmp_path / "reject.ll"),
                             module_names=["raw_provider", "entry"], entry_module="entry",
                             backend="self", libpython_mode="off", ir_scaffold_mode="on", emit_llvm_only=True)
    diagnostic = str(caught.value) + "\n" + capfd.readouterr().err
    assert 'compiled module attribute assignment to raw typed storage is not supported' in diagnostic, diagnostic


@pytest.fixture
def explicit_runtime():
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest
    requested = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert requested, "explicit matching threaded runtime required; never build implicitly"
    runtime = Path(requested).resolve(strict=True)
    verify_runtime_archive_manifest(runtime, runtime_root=_ROOT / "pcc/runtime")
    return runtime


@pytest.mark.integration
@pytest.mark.parametrize("lifetime", (False, True), ids=("bindings", "finalizer"))
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_host_emitted_pointer_module_attributes(tmp_path, monkeypatch, explicit_runtime, scaffold, lifetime):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch, explicit_runtime)
    binary = tmp_path / "module-storage"
    compile_python_multi(_sources(tmp_path, lifetime), str(binary),
                         module_names=["storage_provider", "storage_observer", "storage_entry"],
                         entry_module="storage_entry", backend="self", libpython_mode="off",
                         ir_scaffold_mode=scaffold, runtime_archive=str(explicit_runtime))
    expected = "module-pointer-lifetime-ok\n" if lifetime else "module-pointer-storage-ok\n"
    for backend in (1, 4):
        result = subprocess.run([str(binary), str(backend)], cwd=tmp_path, capture_output=True, text=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"))
        (tmp_path / ("execute-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("execute-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == expected, (backend, result.stdout, result.stderr)


def test_unsafe_provider_pointer_is_not_a_managed_mutation_target(tmp_path, monkeypatch, capfd):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    _configure(monkeypatch)
    provider = tmp_path / "raw_owner.py"
    provider.write_text("from pcc.unsafe import malloc\nBUFFER = malloc(32)\n")
    _, exports, _ = build_closed_world_context([str(provider)], ["raw_owner"], merge_exports=False)
    assert exports["raw_owner"]["BUFFER"]["storage_owner"] == "unknown"
    control = tmp_path / "control.py"
    control.write_text("import raw_owner\ndef main():\n    return 0\nmain()\n")
    output = tmp_path / "raw-provider.ll"
    compile_python_multi([str(provider), str(control)], str(output),
                         module_names=["raw_owner", "control"], entry_module="control",
                         backend="self", libpython_mode="off", emit_llvm_only=True)
    text = output.read_text()
    # This producer exposes raw addresses as i64 (the export's shallow Dyn
    # descriptor does not authorize treating those bits as a PyObject).
    raw = re.search(r"(%[\w.]+) = call ptr \(i64\) @malloc\(i64 32\)", text)
    assert raw is not None, "missing owned malloc producer call"
    address = re.search(r"(%[\w.]+) = ptrtoint ptr " + re.escape(raw.group(1)) + r" to i64", text)
    assert address is not None, "missing raw address projection"
    assert "store i64 " + address.group(1) + ", ptr @.modvar.raw_owner.BUFFER" in text
    for line in text.splitlines():
        if "call " in line and ("@pcc_gc_store_root(" in line or "@pcc_gc_pin(" in line):
            assert raw.group(1) not in line and address.group(1) not in line, line
    control.write_text("import raw_owner\ndef main():\n    raw_owner.BUFFER = []\nmain()\n")
    with pytest.raises(Exception) as caught:
        compile_python_multi([str(provider), str(control)], str(tmp_path / "reject-raw-owner.ll"),
                             module_names=["raw_owner", "control"], entry_module="control",
                             backend="self", libpython_mode="off", emit_llvm_only=True)
    diagnostic = str(caught.value) + "\n" + capfd.readouterr().err
    assert 'requires proven managed provider storage' in diagnostic, diagnostic


def test_unsafe_rhs_cannot_become_a_managed_module_pointer(tmp_path, monkeypatch, capfd):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    provider = tmp_path / "object_owner.py"
    provider.write_text("TOKEN = None\n")
    entry = tmp_path / "entry.py"
    entry.write_text("from pcc.unsafe import malloc\nimport object_owner\ndef main():\n    object_owner.TOKEN = malloc(32)\nmain()\n")
    with pytest.raises(Exception) as caught:
        compile_python_multi([str(provider), str(entry)], str(tmp_path / "reject-raw-rhs.ll"),
                             module_names=["object_owner", "entry"], entry_module="entry",
                             backend="self", libpython_mode="off", emit_llvm_only=True)
    diagnostic = str(caught.value) + "\n" + capfd.readouterr().err
    assert 'cannot store an unsafe raw pointer as a Python object' in diagnostic, diagnostic
