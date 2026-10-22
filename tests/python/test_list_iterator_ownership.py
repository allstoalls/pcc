"""list(iterable) must consume iterator/items without retaining its input graph."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("shape", ["borrowed", "temporary", "enumerate", "enumerate_temporary", "raising", "source_lifetime"])
def test_list_iterator_releases_owners_on_success_and_error(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, shape,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    bodies = {
        "borrowed": "xs = build(count)\n    ys = list(xs)\n    assert ys is not xs\n    assert len(xs) == len(ys) == count\n    assert ys[0] is xs[0]",
        "temporary": "ys = list(build(count))\n    assert len(ys) == count",
        "enumerate": "xs = build(count)\n    ys = list(enumerate(xs))\n    assert len(ys) == count\n    assert ys[0][0] == 0\n    assert ys[0][1] is xs[0]",
        "enumerate_temporary": "pairs = enumerate(build(count))\n    ys = [pair for pair in pairs]\n    assert len(ys) == count\n    assert ys[0][0] == 0",
        "raising": "try:\n        ys = list(Broken(count))\n    except ValueError as exc:\n        assert str(exc) == 'iterator failure'\n    else:\n        assert False\n    try:\n        ys = list(NoIterator())\n    except ValueError as exc:\n        assert str(exc) == 'iter failure'\n    else:\n        assert False",
        "source_lifetime": "global source_destroyed\n    source_destroyed = 0\n    ys = list(Source(count))\n    assert len(ys) == count\n    gc.collect()\n    assert source_destroyed == 1",
    }
    source = tmp_path / "list_owners.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
destroyed = 0
source_destroyed = 0
class Item:
    def __init__(self, value):
        self.value = value
    def __del__(self):
        global destroyed
        destroyed += 1
def build(count):
    return [Item(i) for i in range(count)]
class Broken:
    def __init__(self, count):
        self.count = count
        self.index = 0
    def __iter__(self):
        return self
    def __next__(self):
        if self.index == self.count:
            raise ValueError("iterator failure")
        self.index += 1
        return Item(self.index)
class NoIterator:
    def __iter__(self):
        raise ValueError("iter failure")
class IndependentIterator:
    def __init__(self, count):
        self.count = count
        self.index = 0
    def __iter__(self):
        return self
    def __next__(self):
        assert source_destroyed == 0
        if self.index == self.count:
            raise StopIteration
        self.index += 1
        return Item(self.index)
class Source:
    def __init__(self, count):
        self.count = count
    def __iter__(self):
        return IndependentIterator(self.count)
    def __del__(self):
        global source_destroyed
        source_destroyed += 1
def exercise(count):
    BODY
def main():
    exercise(2000)
    gc.collect()
    assert destroyed == 2000
    before = heap()
    exercise(2000)
    gc.collect()
    assert destroyed == 4000
    print(destroyed, heap() - before)
main()
'''.replace("BODY", bodies[shape]))
    binary = tmp_path / "list_owners"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=25,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        destroyed, growth = map(int, result.stdout.split())
        assert destroyed == 4000
        if backend == 0:
            assert growth < 16384, growth
