"""Runtime any/all walks own their source until every exit has finished."""

import os
import subprocess

import pytest


def _run(source, tmp_path, compiler, runtime):
    path = tmp_path / "any_all_owner.py"
    path.write_text(source)
    binary = tmp_path / "any_all_owner"
    compiler(str(path), str(binary), backend="self", libpython_mode="off",
             runtime_archive=str(runtime))
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        yield backend, result.stdout


@pytest.mark.parametrize("builtin", ["any", "all"])
def test_any_all_release_temporary_and_preserve_borrowed_sources(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, builtin,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = '''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def dictionary(value) -> dict:
    return {value: 42}
def scan(count, empty, values):
    for repeat in range(count):
        assert BUILTIN(value > 0 for value in empty) == EMPTY
        assert BUILTIN(value > 0 for value in values) == NONEMPTY
        assert not BUILTIN(list([0, 0]))
        assert BUILTIN(tuple([1, 1]))
        assert not BUILTIN(dictionary(0))
        assert BUILTIN(dictionary(1))
        assert BUILTIN(values) == NONEMPTY
        assert values == [0, 1]
def main():
    empty = []
    values = [0, 1]
    scan(2000, empty, values)
    gc.collect()
    before = heap()
    scan(2000, empty, values)
    gc.collect()
    print(heap() - before)
main()
'''.replace("BUILTIN", builtin).replace("NONEMPTY", str(builtin == "any")).replace(
        "EMPTY", str(builtin == "all"))
    for backend, output in _run(source, tmp_path, python_program_compiler, pcc_runtime_archive):
        if backend == 0:
            assert int(output) < 16384, output


@pytest.mark.parametrize("builtin", ["any", "all"])
def test_any_all_release_sources_on_error_and_callback_rebinding(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, builtin,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = '''import gc
destroyed = 0
anchor = None
class Bomb:
    def __bool__(self):
        raise ValueError("truth")
class Items:
    def __init__(self, mode):
        self.mode = mode
        self.done = False
    def __iter__(self):
        return self
    def __next__(self):
        if self.mode == 1:
            raise ValueError("item")
        if self.done:
            raise StopIteration
        self.done = True
        if self.mode == 2:
            return Bomb()
        return True
class Source:
    def __init__(self, mode: int):
        self.mode = mode
    def __del__(self):
        global destroyed
        destroyed += 1
    def __iter__(self):
        global anchor
        if self.mode == 0:
            raise ValueError("iter")
        if self.mode == 4:
            return 5
        if self.mode == 3:
            anchor = None
            gc.collect()
        return Items(self.mode)
def make(mode) -> object:
    return Source(mode)
def main():
    global anchor
    caught = 0
    for repeat in range(32):
        for mode in range(3):
            try:
                BUILTIN(make(mode))
            except ValueError:
                caught += 1
        try:
            BUILTIN(make(4))
        except TypeError:
            caught += 1
        anchor = make(3)
        assert BUILTIN(anchor)
    gc.collect()
    print(caught, destroyed)
main()
'''.replace("BUILTIN", builtin)
    for backend, output in _run(source, tmp_path, python_program_compiler, pcc_runtime_archive):
        assert output == "128 160\n", (backend, output)
