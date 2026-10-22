"""A raw i64 module global updated through ``global n; n += ...`` in a function.

Importing ``pcc.extern`` gives module-level ints raw i64 storage. An
augmented assignment in a function boxes the loaded value, so its result is
an object (a tagged int, or a heap int past the tagged lane). The module
global store used to treat that result as an object slot: it loaded the old
i64 as a pointer for ``pcc_gc_unpin`` and rooted the new pointer in the i64
cell (rejected by the self IR verifier as an operand-type error; otherwise the
cell would hold tagged object bits). The store now unboxes into the cell.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


SOURCE = '''from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
count = 0
total = 0


class Item:
    def __init__(self):
        global count
        count += 1


def bump(step):
    global total
    total += step


def main():
    for _ in range(1000):
        Item()
    bump(1)
    bump(2 ** 62)
    bump(-(2 ** 62))
    bump(4611686018427387903)
    print(count, total)


main()
'''


def _write(directory):
    path = directory / "raw_int_globals.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def test_module_global_store_unboxes_into_raw_cell(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE", "0")
    source = _write(tmp_path)
    out_ll = tmp_path / "raw_int_globals.ll"
    compile_python(str(source), str(out_ll), backend="self", libpython_mode="off",
                   emit_llvm_only=True)
    text = out_ll.read_text()
    assert "@.modvar.raw_int_globals.count = global i64 0" in text
    for name in ("count", "total"):
        cell = re.escape("@.modvar.raw_int_globals." + name)
        assert not re.search(r"@pcc_gc_store_root\([^\n]*" + cell + r"\b", text), name
        assert not re.search(r"load i64, ptr " + cell + r"\n[^\n]*@pcc_gc_unpin", text), name
        assert re.search(r"store i64 [^,]+, ptr " + cell + r"\b", text), name


def test_reference_output(tmp_path):
    source = _write(tmp_path)
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    result = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, timeout=20, env=environment, cwd=tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, "1000 4611686018427387904\n", "")


@pytest.mark.integration
def test_raw_int_module_globals_execute_on_five_gcs(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python
    from pcc.tools.runtime_archive_provenance import manifest_is_stale_for_current_codegen

    manifest = json.loads(Path(str(pcc_runtime_archive) + ".provenance.json").read_text())
    assert not manifest_is_stale_for_current_codegen(manifest), "runtime/codegen identity mismatch"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE", "0")
    monkeypatch.setenv("PCC_SELF_BACKEND_OBJECT_CACHE", "0")
    source = _write(tmp_path)
    binary = tmp_path / "raw_int_globals"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent")
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30, env=environment, cwd=tmp_path)
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "1000 4611686018427387904\n", ""), (backend, result.stdout, result.stderr)
