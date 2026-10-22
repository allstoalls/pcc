"""Native sorted results are owned even in raw-ABI and branch joins."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("fail_iteration", [False, True])
def test_sorted_without_key_releases_temporary_iterable(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
    fail_iteration,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "sorted_plain_input.py"
    source.write_text('''import gc
destroyed = 0
class Source:
    def __iter__(self):
        gc.collect()
        assert destroyed == 0
        if FAIL:
            raise ValueError("iteration failed")
        return iter([3, 1, 2])
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
def main():
    try:
        result = sorted(Source(), reverse=True)
    except ValueError as exc:
        assert FAIL
        assert str(exc) == "iteration failed"
    else:
        assert not FAIL
        assert result == [3, 2, 1]
    gc.collect()
    assert destroyed == 1
    print("plain-source-ok")
main()
'''.replace("FAIL", str(fail_iteration)))
    binary = tmp_path / "sorted_plain_input"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.strip() == "plain-source-ok"


@pytest.mark.parametrize("source_shape", ["call", "attribute", "borrowed"])
def test_sorted_key_releases_temporary_input_owner(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, source_shape,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    setup, value = {
        "call": ("pass", "make()"),
        "attribute": ("holder = Holder(make())", "holder.values"),
        "borrowed": ("values = make()", "values"),
    }[source_shape]
    source = tmp_path / "sorted_input_owner.py"
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
class Holder:
    def __init__(self, values: list):
        self.values = values
def make() -> list:
    return [Item(i) for i in range(1000)]
def run():
    SETUP
    result = sorted(VALUE, key=lambda item: item.index, reverse=True)
    assert len(result) == 1000
    assert result[0].index == 999
    assert result[-1].index == 0
    gc.collect()
    assert created - destroyed == 1000
def main():
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created == 1000
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created == 2000
    print(created, destroyed, heap() - before)
main()
'''.replace("SETUP", setup).replace("VALUE", value))
    binary = tmp_path / "sorted_input_owner"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        created, destroyed, growth = map(int, ran.stdout.split())
        assert created == destroyed == 2000
        if backend == 0:
            assert growth < 16384, growth


@pytest.mark.parametrize("fail_key", [False, True])
def test_sorted_input_outlives_callbacks_and_is_released_on_error(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, fail_key,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "sorted_input_lifetime.py"
    source.write_text('''import gc
destroyed = 0
class Source:
    def __init__(self):
        self.values = [3, 1, 2]
    def __iter__(self):
        return iter(self.values)
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
def rank(value):
    assert destroyed == 0
    gc.collect()
    if FAIL_KEY:
        raise ValueError("key failed")
    return value
def main():
    try:
        result = sorted(Source(), key=rank)
    except ValueError as exc:
        assert FAIL_KEY
        assert str(exc) == "key failed"
    else:
        assert not FAIL_KEY
        assert result == [1, 2, 3]
    gc.collect()
    assert destroyed == 1
    print("source-lifetime-ok")
main()
'''.replace("FAIL_KEY", str(fail_key)))
    binary = tmp_path / "sorted_input_lifetime"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.strip() == "source-lifetime-ok"


@pytest.mark.parametrize("expression", [
    "sorted(values)", "sorted(values, key=lambda x: -x)",
    "sorted(values, reverse=True)",
])
def test_sorted_result_is_released_and_generator_join_keeps_range_owner(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, expression,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "sorted_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def sequence(values, mode):
    if mode == 0:
        indices = range(len(values))
    else:
        indices = EXPRESSION
    for index in indices:
        yield index
def scan(values, mode):
    total = 0
    for index in sequence(values, mode):
        total += index
    assert total == len(values) * (len(values) - 1) // 2
def main():
    values = list(range(10000))
    for mode in range(2):
        scan(values, mode)
        for repeat in range(2):
            gc.collect()
            before = heap()
            scan(values, mode)
            gc.collect()
            print(heap() - before)
main()
'''.replace("EXPRESSION", expression))
    binary = tmp_path / "sorted_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        growth = list(map(int, result.stdout.split()))
        if backend == 0:
            assert all(value < 16384 for value in growth), growth


def test_sorted_custom_comparison_releases_result_storage(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "sorted_custom_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Item:
    def __init__(self, value: int):
        self.value = value
    def __lt__(self, other):
        return self.value < other.value
def scan(count):
    values = [Item(3), Item(1), Item(2)]
    for repeat in range(count):
        ordered = sorted(values)
        first = ordered[0]
        last = ordered[2]
        assert first.value == 1
        assert last.value == 3
    original_first = values[0]
    assert original_first.value == 3
def main():
    scan(16)
    gc.collect()
    before = heap()
    scan(2000)
    gc.collect()
    print(heap() - before)
main()
''')
    binary = tmp_path / "sorted_custom_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        if backend == 0:
            assert int(result.stdout) < 16384, result.stdout
