"""Global barriers and nonlocal cells in emitted native closure programs.

These tests use the host frontend explicitly. They do not establish that a
native pcc1 producer includes this hoist change. Runtime construction is never
implicit; execution requires PCC_RUNTIME_ARCHIVE to name a matching archive.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROVIDER = 'VALUE = ("imported",)\nSECOND = ("nonlocal-imported",)\n'
PROGRAM = '''import gc
X = ("module-initial",)

def set_global(value):
    global X
    X = value

def import_global_reader():
    global X
    from scope_provider import VALUE as X
    def global_import_read():
        return X
    return global_import_read

def global_barrier_factory():
    X = ("outer-local",)
    def middle():
        global X
        def global_barrier_read():
            return X
        return global_barrier_read
    return middle()

def explicit_global_factory(X):
    def explicit_global_read():
        global X
        return X
    def local_read():
        return X
    X = ("parameter-rebound",)
    return explicit_global_read, local_read

def nonlocal_factory():
    X = ("nonlocal-initial",)
    def middle():
        nonlocal X
        from scope_provider import SECOND as X
        def nonlocal_read():
            return X
        def nonlocal_write(value):
            nonlocal X
            X = value
        return nonlocal_read, nonlocal_write
    return middle()

class ReaderFactory:
    def make(self):
        global X
        from scope_provider import VALUE as X
        def method_global_read():
            return X
        return method_global_read

def main():
    imported = import_global_reader()
    assert imported() == ("imported",)
    barrier = global_barrier_factory()
    global_read, local_read = explicit_global_factory(("parameter",))
    method_read = ReaderFactory().make()
    reader, writer = nonlocal_factory()
    assert reader() == ("nonlocal-imported",)
    set_global(("module-rebound",))
    writer(("nonlocal-rebound",))
    gc.collect()
    assert imported() == ("module-rebound",)
    assert barrier() == ("module-rebound",)
    assert global_read() == ("module-rebound",)
    assert method_read() == ("module-rebound",)
    assert local_read() == ("parameter-rebound",)
    assert reader() == ("nonlocal-rebound",)
    assert X == ("module-rebound",)
    print("hoist-global-cell-boundaries-ok")
main()
'''


def _sources(tmp_path):
    provider = tmp_path / "scope_provider.py"
    provider.write_text(PROVIDER)
    entry = tmp_path / "scope_entry.py"
    entry.write_text(
        "from pcc.extern import extern, c_int64\n"
        "import sys\n"
        "_backend = extern('pcc_gc_backend', (), c_int64)\n"
        "assert _backend() == int(sys.argv[1])\n" + PROGRAM
    )
    return [str(provider), str(entry)]


def _configure(monkeypatch):
    for key, value in {
        "PCC_PYTHON_IR_PASSES": "off", "PCC_PY_FRONTEND_JOBS": "1",
        "PCC_SELF_BACKEND_JOBS": "1", "PCC_SELF_LINK": "pcc",
        "PCC_NO_AUTO_PCC1": "1", "PCC_WITH_THREADS": "1",
        "PCC_SELF_BACKEND_OBJECT_CACHE": "0", "PCC_PY_FRONTEND_IR_CACHE": "0",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_host_ir_global_reads_do_not_become_capture_values(tmp_path, monkeypatch, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi

    _configure(monkeypatch)
    output = tmp_path / "scopes.ll"
    compile_python_multi(
        _sources(tmp_path), str(output),
        module_names=["scope_provider", "scope_entry"], entry_module="scope_entry",
        emit_llvm_only=True, backend="self", libpython_mode="off",
        ir_scaffold_mode=scaffold,
    )
    text = output.read_text()
    for name in ("global_import_read", "global_barrier_read",
                 "explicit_global_read", "method_global_read"):
        matches = re.findall(
            r"^define[^\n]*@[^\n(]*__nested_" + name
            + r"(?:_\d+)?\(([^\n]*)\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S,
        )
        assert len(matches) == 1, (name, len(matches))
        params, body = matches[0]
        # The global is read from the module when the function runs: its
        # module variable, or the module attribute once an import statement
        # made the binding live. It never arrives as a captured value.
        assert ("@.modvar.scope_entry.X" in body
                or ("@py_module_attr_get(" in body and "name.dynamic.X" in body)), (name, body)
        assert "%X" not in params, (name, params)


@pytest.mark.integration
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_host_emitted_global_barrier_and_nonlocal_cells(tmp_path, monkeypatch, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

    requested = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert requested, "set an explicit matching prebuilt runtime; no auto-build"
    runtime = Path(requested).resolve(strict=True)
    verify_runtime_archive_manifest(runtime, runtime_root=ROOT / "pcc/runtime")
    _configure(monkeypatch)
    binary = tmp_path / "scopes"
    compile_python_multi(
        _sources(tmp_path), str(binary),
        module_names=["scope_provider", "scope_entry"], entry_module="scope_entry",
        backend="self", libpython_mode="off", ir_scaffold_mode=scaffold,
        runtime_archive=str(runtime),
    )
    for backend in (1, 4):
        result = subprocess.run(
            [str(binary), str(backend)], cwd=tmp_path,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"),
            capture_output=True, text=True, timeout=30,
        )
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, (backend, result.returncode, result.stderr)
        assert result.stdout == "hoist-global-cell-boundaries-ok\n", result.stdout
