"""Native callable keys and lambda adapter reads have bounded ownership."""

import os
import subprocess
import textwrap

import pytest


@pytest.mark.parametrize("shape", [
    "typed", "dynamic", "sorted", "borrowed", "factory", "callable",
    "empty", "raising", "collect", "identity", "capture_return", "default",
])
def test_native_sort_key_and_lambda_owners(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, shape,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    bodies = {
        "typed": "order.sort(key=lambda index: (payload[index].index,))",
        "dynamic": "order.sort(key=lambda index: (payload[index].index,))",
        "sorted": "original = order\norder = sorted(original, key=lambda index: (payload[index].index,))\nassert original == [2, 0, 1]",
        "borrowed": "key = lambda index: (payload[index].index,)\norder.sort(key=key)\nassert key(2) == (2,)",
        "factory": "order.sort(key=factory(payload))",
        "callable": "order.sort(key=Key(payload, order))\nassert published[-1] == [0, 1, 2]",
        "empty": "order.clear()\norder.sort(key=lambda index: (payload[index].index,))",
        "raising": "try:\n    order.sort(key=lambda index: fail(payload, index))\nexcept ValueError as exc:\n    assert str(exc) == 'key failed'\nelse:\n    assert False",
        "collect": "order.sort(key=lambda index: collected(payload, index))",
        "identity": "functions = [lambda value: value]\nresult = functions[0](payload[0])\ndel payload\ngc.collect()\nassert result.index == 0",
        "default": "item = payload[0]\nfunctions = [lambda value=item: value]\nresult = functions[0]()\nassert result is item\ndel payload\ndel item\ngc.collect()\nassert result.index == 0",
        "capture_return": "functions = [lambda: payload]\nresult = functions[0]()\nassert result is payload\ndel payload\ngc.collect()\nassert len(result) == 300",
    }
    body = bodies[shape]
    if shape not in ("empty", "raising", "identity", "default", "capture_return"):
        body += "\nassert order == [0, 1, 2]"
    source = tmp_path / "key_owners.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
created = 0
destroyed = 0
published = []
class Item:
    def __init__(self, index):
        global created
        self.index = index
        self.payload = b"x" * 1024
        created += 1
    def __del__(self):
        global destroyed
        destroyed += 1
class Key:
    def __init__(self, payload, order):
        self.payload = payload
        self.order = order
    def __call__(self, index):
        return (self.payload[index].index,)
    def __del__(self):
        published.append(list(self.order))
def factory(payload):
    return lambda index: (payload[index].index,)
def fail(payload, index):
    assert payload[index].index == index
    raise ValueError("key failed")
def collected(payload, index):
    gc.collect()
    return (payload[index].index,)
def exercise(order: list):
    payload = [Item(i) for i in range(300)]
    BODY
def run():
    exercise([2, 0, 1])
def main():
    run()
    gc.collect()
    gc.collect()
    assert created == destroyed
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert created == destroyed
    print(created, destroyed, heap() - before)
main()
'''.replace("    BODY\n", textwrap.indent(body, "    ") + "\n").replace(
        "def exercise(order: list):",
        "def exercise(order):" if shape == "dynamic" else "def exercise(order: list):",
    ))
    binary = tmp_path / "key_owners"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        created, destroyed, retained = map(int, result.stdout.split())
        assert created == destroyed == 600
        if backend == 0:
            assert retained < 16384, retained
