"""Comprehension-local targets and enumerate temporaries own finite lifetimes."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("shape", [
    "typed", "dynamic", "temporary", "filtered", "raising", "shadow",
    "parameter_shadow", "nested", "dict_lifetime", "tuple", "collect",
    "closure_default", "closure",
])
def test_comprehension_owners_leave_scope(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, shape,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    bodies = {
        "typed": "output = [(i, value) for i, value in enumerate(values)]\n    assert len(output) == len(values)\n    assert output[513][0] == 513\n    assert output[513][1] is values[513]",
        "dynamic": "output = dynamic(values)\n    assert len(output) == len(values)",
        "temporary": "output = [(i, value) for i, value in enumerate(build(1000))]\n    assert len(output) == 1000",
        "filtered": "output = [value for i, value in enumerate(values) if i % 2 == 0]\n    assert len(output) == 500",
        "raising": "try:\n        output = [checked(value) for i, value in enumerate(values)]\n    except ValueError:\n        pass\n    else:\n        assert False",
        "shadow": "value = Item(-1)\n    output = [value for i, value in enumerate(values)]\n    assert value.index == -1\n    assert len(output) == 1000",
        "parameter_shadow": "output = [values for i, values in enumerate(values)]\n    assert len(values) == 1000\n    assert output[-1] is values[-1]",
        # 16 inner items keep the nested shape; a per-iteration owner leak
        # still retains >700 KB, while 1000x1000 took GC1 21 s of a 30 s budget.
        "nested": "inner = values[:16]\n    output = [value for i, value in enumerate(values) for j, other in enumerate(inner) if i == 0 and j == 0]\n    assert len(output) == 1",
        "dict_lifetime": "output = [check_key(key) for i, key in enumerate(build_dict(1000))]\n    assert len(output) == 1000",
        "tuple": "output = tuple_case(tuple(values))\n    assert len(output) == 1000",
        "collect": "output = [collected(value) for i, value in enumerate(values)]\n    assert output[-1] is values[-1]",
        "closure_default": "output = [lambda value=value: value for i, value in enumerate(values)]\n    assert output[0]() is values[0]\n    assert output[-1]() is values[-1]",
        "closure": "output = [lambda: value for i, value in enumerate(values)]\n    assert output[0]() is values[-1]\n    assert output[-1]() is values[-1]",
    }
    source = tmp_path / "comp_owners.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
created = 0
destroyed = 0
class Item:
    def __init__(self, index):
        global created
        self.index = index
        self.payload = b"x" * 256
        created += 1
    def __del__(self):
        global destroyed
        destroyed += 1
def build(count) -> list:
    return [Item(i) for i in range(count)]
def dynamic(values):
    return [(i, value) for i, value in enumerate(values)]
def tuple_case(values: tuple):
    return [(i, value) for i, value in enumerate(values)]
def build_dict(count) -> dict:
    return {i: Item(i) for i in range(count)}
def check_key(key):
    assert created - destroyed == 2000
    return key
def checked(value):
    if value.index == 513:
        raise ValueError("stop")
    return value
def collected(value):
    if value.index % 200 == 0:
        gc.collect()
    return value
def operation(values: list):
    destroyed_before = destroyed
    BODY
    if "SHAPE" != "dict_lifetime":
        assert destroyed == destroyed_before
def run():
    values = build(1000)
    operation(values)
def main():
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created
    print(created, destroyed, heap() - before)
main()
'''.replace("BODY", bodies[shape]).replace("SHAPE", shape))
    binary = tmp_path / "comp_owners"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        created, destroyed, retained = map(int, result.stdout.split())
        assert created == destroyed
        if backend == 0:
            assert retained < 8192, retained
