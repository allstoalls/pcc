"""Indirect native calls transfer one owner, including optional callbacks."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("consume", [
    "rows = source()\n        for value in rows:\n            pass",
    "rows = () if source is None else source()\n        for value in rows:\n            pass",
    "drain(source())",
])
def test_optional_generator_callback_does_not_retain_receiver(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, consume,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "callback_owner.py"
    source.write_text('''import gc
from pcc.unsafe import load_i64, abi_constant
destroyed = 0
class Owner:
    def rows(self):
        yield 42
    def __del__(self):
        global destroyed
        destroyed += 1
def drain(rows):
    for value in rows:
        assert value == 42
def consume(source=None):
    for index in range(100):
        BODY
def main():
    owner = Owner()
    before = load_i64(owner, abi_constant("object.header.refcount_offset"))
    consume(source=owner.rows)
    gc.collect()
    print(load_i64(owner, abi_constant("object.header.refcount_offset"))-before)
    owner = None
    gc.collect()
    print(destroyed)
main()
'''.replace("BODY", consume))
    binary = tmp_path / "callback_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == "0\n1\n", (backend, run.stdout)


def test_callable_expression_result_releases_fresh_object(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "expression_result.py"
    source.write_text('''import gc
destroyed = 0
class Result:
    def __del__(self):
        global destroyed
        destroyed += 1
def make():
    return Result()
def consume(source=None):
    for index in range(100):
        result = (source,)[0]()
        result = None
def main():
    consume(make)
    gc.collect()
    print(destroyed)
main()
''')
    binary = tmp_path / "expression_result"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == "100\n", (backend, run.stdout)
