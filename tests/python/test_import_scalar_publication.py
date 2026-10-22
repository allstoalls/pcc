"""Managed import bindings retain scalar ABI projections without GC ownership."""

from __future__ import annotations

import re

import pytest

from pcc.backend import BackendUnavailable
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module
from pcc.ir.compat import ir
from tests.owned_ir_validation import verify_ir_text


def test_import_scalar_cannot_enter_gc_object_helper():
    with pytest.raises(BackendUnavailable, match=r"self IR verifier \[operand-type\]"):
        verify_ir_text('''declare void @pcc_gc_unpin(ptr)
define void @scalar_import() {
entry:
  %current = add i64 37, 0
  call void @pcc_gc_unpin(ptr %current)
  ret void
}
''')


@pytest.mark.parametrize("module_name", ("pcc.consumer", "consumer"))
@pytest.mark.parametrize("value", ("37", "True", "1.25", "object()"))
def test_import_publication_respects_physical_global_storage(
    tmp_path, monkeypatch, module_name, value,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text("value = " + value + "\n")
    consumer.write_text(
        "from provider import value\n"
        "def probe():\n    return value\n"
        "def replace():\n    global value\n    value = " + value + "\n"
        "def erase():\n    global value\n    del value\n"
    )
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(consumer)], ["provider", module_name],
    )
    external = {"provider": exports["provider"]}
    typed = infer_module(modules[-1], external_exports=external, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._native_module_exports = external
    codegen._sibling_module_inits = ("provider",)
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(typed))
    (tmp_path / "consumer.ll").write_text(text)
    verify_ir_text(text)
    assert "name.dynamic.value" in text
    assert "@py_module_attr_del(" in text
    slot = codegen._module_globals["value"][0]
    symbol = "@" + slot.name
    if module_name == "pcc.consumer" and value == "37":
        assert isinstance(slot.value_type, ir.IntType) and slot.value_type.width == 64
    if isinstance(slot.value_type, ir.PointerType):
        assert re.search(r"load ptr, ptr " + re.escape(symbol), text)
        assert "import.publish.current" in text
    else:
        # A scalar projection is an exported ABI value, never a GC root.
        assert re.search(r"store (?:i64|i1|double) [^\n]*, ptr " + re.escape(symbol), text)
        assert not re.search(r"call [^\n]*@pcc_gc_store_root\(ptr " + re.escape(symbol) + r"[,)]", text)
        assert not re.search(r"%import\.publish\.current[^\n]*= load [^\n]*" + re.escape(symbol), text)
    # NEW import results remain in temporary owning slots before callbacks.
    for producer in ("py_compiled_module_import_by_name", "py_obj_getattr"):
        calls = re.findall(r"^\s*(%[\w.]+) = call [^\n]*@" + producer + r"\([^\n]*\)\n([^\n]*)", text, re.M)
        assert calls
        for result, next_line in calls:
            assert re.search(r"store ptr " + re.escape(result) + r", ptr %", next_line)


@pytest.mark.integration
def test_import_scalar_publication_native_binding_readback(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    """Execute import, dictionary/source writes, deletion and reimport."""
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.tools.runtime_archive_provenance import manifest_is_stale_for_current_codegen

    manifest = json.loads(Path(str(pcc_runtime_archive) + ".provenance.json").read_text())
    assert not manifest_is_stale_for_current_codegen(manifest), "runtime/codegen identity mismatch"
    package = tmp_path / "pcc"
    package.mkdir()
    sources = [
        (tmp_path / "provider.py", "provider", "value = 37\n"),
        (package / "__init__.py", "pcc", ""),
        (package / "consumer.py", "pcc.consumer", '''from provider import value

def probe():
    return value

def replace():
    global value
    value = 41

def erase():
    global value
    del value

def reimport():
    global value
    from provider import value
'''),
        (tmp_path / "entry.py", "entry", '''import pcc.consumer as consumer

def main():
    assert consumer.probe() == 37
    namespace = consumer.__dict__
    namespace['value'] = 53
    assert consumer.probe() == 53
    consumer.replace()
    assert consumer.probe() == 41
    assert namespace['value'] == 41
    consumer.erase()
    assert 'value' not in namespace
    try:
        consumer.probe()
    except NameError:
        pass
    else:
        raise AssertionError('deleted scalar import remains readable')
    namespace['value'] = 67
    assert consumer.probe() == 67
    consumer.reimport()
    assert consumer.probe() == 37
    assert namespace['value'] == 37
    print('IMPORT_SCALAR_PUBLICATION_OK')
main()
'''),
    ]
    for path, _name, source in sources:
        path.write_text(source)
    expected = "IMPORT_SCALAR_PUBLICATION_OK\n"
    reference_env = dict(os.environ)
    reference_env.pop("PYTHONPATH", None)
    reference = subprocess.run(
        [sys.executable, "-B", str(sources[-1][0])], cwd=tmp_path,
        env=reference_env, capture_output=True, text=True, timeout=15,
    )
    assert (reference.returncode, reference.stdout, reference.stderr) == (0, expected, "")
    for name, value in {
        "PCC_PYTHON_IR_PASSES": "off", "PCC_PY_FRONTEND_JOBS": "1",
        "PCC_SELF_BACKEND_JOBS": "1", "PCC_NO_AUTO_PCC1": "1",
        "PCC_SELF_LINK": "pcc", "PCC_SELF_BACKEND_OBJECT_CACHE": "0",
        "PCC_PY_FRONTEND_IR_CACHE": "0", "PCC_RUNTIME_ARCHIVE": str(pcc_runtime_archive),
    }.items():
        monkeypatch.setenv(name, value)
    binary = tmp_path / "scalar-import"
    compile_python_multi(
        [str(path) for path, _name, _source in sources], str(binary),
        module_names=[name for _path, name, _source in sources], entry_module="entry",
        backend="self", libpython_mode="off", ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent")
        environment.pop("LC_ALL", None)
        result = subprocess.run(
            [str(binary)], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30,
        )
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert (result.returncode, result.stdout, result.stderr) == (0, expected, ""), backend
