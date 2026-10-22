"""Native sibling from-imports create owned locals, not uninitialized globals."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = '''VALUES = ("mem2reg", "sroa")
NUMBER = 7
ITEMS = []
TOKEN = None
'''
SECOND = '''VALUES = ("replacement",)
'''
BINDINGS = '''import import_provider as provider
import gc
VALUES = ["module-sentinel"]
GLOBAL_VALUE = ("global-sentinel",)
GLOBAL_ITEMS = ["global-list-sentinel"]

def bind_global():
    global GLOBAL_VALUE
    from import_provider import VALUES as GLOBAL_VALUE

def bind_global_items():
    global GLOBAL_ITEMS
    from import_provider import ITEMS as GLOBAL_ITEMS

def fresh_number():
    from import_provider import NUMBER
    return NUMBER

def fetch():
    from import_provider import VALUES
    return list(VALUES)

def overwrite():
    value = 41
    from import_provider import VALUES as value
    assert list(value) == ["mem2reg", "sroa"]
    value = ["local-reassignment"]
    return value

def scalar():
    from import_provider import NUMBER as count
    assert count == 7
    count = 9
    return count

def closure():
    from import_provider import VALUES
    def read():
        return VALUES
    from import_second import VALUES
    return read

def closure_before_import():
    def read():
        return VALUES
    from import_provider import VALUES
    return read

def assignment_closure():
    VALUES = ("assignment-first",)
    def read():
        return VALUES
    VALUES = ("assignment-last",)
    return read

def parameter_closure(VALUES):
    def read():
        return VALUES
    VALUES = ("parameter-last",)
    return read

def mutable_alias():
    from import_provider import ITEMS
    ITEMS.append("through-local")
    return ITEMS

def main():
    assert fetch() == ["mem2reg", "sroa"]
    assert overwrite() == ["local-reassignment"]
    assert scalar() == 9
    read = closure()
    read_before = closure_before_import()
    gc.collect()
    assert read() == ("replacement",)
    assert read_before() == ("mem2reg", "sroa")
    assigned = assignment_closure()
    parameter = parameter_closure(("parameter-first",))
    gc.collect()
    assert assigned() == ("assignment-last",)
    assert parameter() == ("parameter-last",)
    assert mutable_alias() is provider.ITEMS
    assert provider.ITEMS == ["through-local"]
    assert VALUES == ["module-sentinel"]
    assert fresh_number() == 7
    provider.VALUES = ("rebound",)
    provider.NUMBER = 11
    assert fetch() == ["rebound"]
    assert fresh_number() == 11
    assert VALUES == ["module-sentinel"]
    assert GLOBAL_VALUE == ("global-sentinel",)
    bind_global()
    assert GLOBAL_VALUE is provider.VALUES
    assert GLOBAL_ITEMS == ["global-list-sentinel"]
    bind_global_items()
    assert GLOBAL_ITEMS is provider.ITEMS
    print("local-from-import-bindings-ok")
main()
'''
LIFETIMES = '''import import_provider as provider
import weakref
import gc
events = []

class Tracked:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        events.append(self.label)
        gc.collect()

def overwrite_owner():
    value = Tracked("replaced")
    old = weakref.ref(value)
    from import_provider import VALUES as value
    assert old() is None
    assert value == ("mem2reg", "sroa")

def fail_after_binding():
    from import_provider import TOKEN
    provider.TOKEN = None
    assert TOKEN.label == "unwound"
    raise ValueError("after-local-import")

def partial_import_failure():
    try:
        from import_provider import VALUES, absent_export
    except Exception as error:
        # This node checks partial binding and cleanup. The existing runtime's
        # missing-export exception classification is a separate contract gate.
        assert "absent_export" in str(error)
        assert VALUES == ("mem2reg", "sroa")
        return 1
    return 0

def capture_owned():
    from import_provider import TOKEN
    def read():
        return TOKEN
    provider.TOKEN = None
    return read

def main():
    overwrite_owner()
    assert events == ["replaced"]
    provider.TOKEN = Tracked("unwound")
    reference = weakref.ref(provider.TOKEN)
    try:
        fail_after_binding()
    except ValueError as error:
        assert str(error) == "after-local-import"
    else:
        raise AssertionError("exception lost")
    gc.collect()
    assert reference() is None
    assert events == ["replaced", "unwound"]
    assert partial_import_failure() == 1
    provider.TOKEN = Tracked("captured")
    reference = weakref.ref(provider.TOKEN)
    callback = capture_owned()
    gc.collect()
    assert reference() is not None
    value = callback()
    assert value.label == "captured"
    value = None
    callback = None
    gc.collect()
    assert reference() is None
    assert events == ["replaced", "unwound", "captured"]
    print("local-from-import-lifetimes-ok")
main()
'''


def _configure(monkeypatch, runtime=None):
    for key, value in {
        "PCC_PYTHON_IR_PASSES": "off", "PCC_PY_FRONTEND_JOBS": "1",
        "PCC_SELF_BACKEND_JOBS": "1", "PCC_SELF_LINK": "pcc",
        "PCC_WITH_THREADS": "1", "PCC_NO_AUTO_PCC1": "1",
        "PCC_SELF_BACKEND_OBJECT_CACHE": "0", "PCC_PY_FRONTEND_IR_CACHE": "0",
    }.items():
        monkeypatch.setenv(key, value)
    if runtime is not None:
        monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(runtime))


def _sources(tmp_path, lifetime):
    providers = []
    for name, text in (("import_provider", PROVIDER), ("import_second", SECOND)):
        path = tmp_path / (name + ".py")
        path.write_text(text)
        providers.append(str(path))
    entry = tmp_path / "import_entry.py"
    prefix = ("from pcc.extern import extern, c_int64\n"
              "import sys\n"
              "_probe_threads = extern('pcc_threads_enabled', (), c_int64)\n"
              "_probe_backend = extern('pcc_gc_backend', (), c_int64)\n"
              "assert _probe_threads() == 1\n"
              "assert _probe_backend() == int(sys.argv[1])\n")
    entry.write_text(prefix + (LIFETIMES if lifetime else BINDINGS))
    return providers + [str(entry)]


@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_function_import_real_ir_binds_owned_local(tmp_path, monkeypatch, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi

    _configure(monkeypatch)
    output = tmp_path / "imports.ll"
    compile_python_multi(_sources(tmp_path, False), str(output),
                         module_names=["import_provider", "import_second", "import_entry"],
                         entry_module="import_entry", emit_llvm_only=True,
                         backend="self", libpython_mode="off", ir_scaffold_mode=scaffold)
    text = output.read_text()
    function = re.search(r"^define[^\n]*@[^\n(]*_fetch\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert function is not None
    body = function.group(1)
    assert "@py_compiled_module_import_by_name(" in body
    assert "@py_obj_getattr(" in body
    assert "compiled.import.value" in body
    assert "@.modvar.import_entry.VALUES" not in body, body
    main_body = re.search(r"^define[^\n]*@[^\n(]*_main\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert main_body is not None
    assert "strict.nolib.stub" not in main_body.group(1), main_body.group(1)


@pytest.fixture
def explicit_runtime():
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

    requested = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert requested, "set explicit prebuilt matching runtime; no auto-build"
    runtime = Path(requested).resolve(strict=True)
    verify_runtime_archive_manifest(runtime, runtime_root=_ROOT / "pcc/runtime")
    return runtime


@pytest.mark.integration
@pytest.mark.parametrize("lifetime", (False, True), ids=("bindings", "lifetimes"))
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_host_emitted_local_import_scopes_and_owners(tmp_path, monkeypatch, explicit_runtime, lifetime, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi

    _configure(monkeypatch, explicit_runtime)
    binary = tmp_path / "imports"
    compile_python_multi(_sources(tmp_path, lifetime), str(binary),
                         module_names=["import_provider", "import_second", "import_entry"],
                         entry_module="import_entry", backend="self", libpython_mode="off",
                         ir_scaffold_mode=scaffold, runtime_archive=str(explicit_runtime))
    expected = "local-from-import-lifetimes-ok\n" if lifetime else "local-from-import-bindings-ok\n"
    for backend in (1, 4):
        result = subprocess.run([str(binary), str(backend)], cwd=tmp_path, capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"))
        (tmp_path / ("execute-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("execute-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == expected, (backend, result.stdout, result.stderr)
