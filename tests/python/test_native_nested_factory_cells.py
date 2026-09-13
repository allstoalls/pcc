"""Closure cells belong to each lexical invocation, including nested factories."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_nested_sibling_captures_the_materialized_function_binding(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "sibling_function.py"
    source.write_text('''import gc
def factory(label):
    def first():
        return label
    first.label = label
    def second():
        return first, first(), first.label
    return second
def recursive_factory():
    def first(n):
        if n == 0:
            return 0
        return first(n - 1) + 1
    original = first
    def call(n):
        return first(n)
    def replace(fn):
        nonlocal first
        first = fn
    return call, replace, original
def replacement(n):
    return n + 10
def forward_factory():
    def get():
        return later()
    def later():
        return 42
    return get
def mutual_factory():
    def even(n):
        if n == 0:
            return True
        return odd(n - 1)
    def odd(n):
        if n == 0:
            return False
        return even(n - 1)
    return even, odd
def main():
    left = factory("left")
    right = factory("right")
    gc.collect()
    a, av, aa = left()
    b, bv, ba = right()
    print(av, aa, bv, ba, a is b)
    a.label = "changed"
    again, value, attribute = left()
    print(again is a, value, attribute)
    call, replace, original = recursive_factory()
    print(call(3))
    replace(replacement)
    gc.collect()
    print(call(3), original(1))
    forward = forward_factory()
    print(forward())
    even, odd = mutual_factory()
    gc.collect()
    print(even(4), odd(4), even(5), odd(5))
main()
''')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "sibling_function"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(output)], capture_output=True, text=True, timeout=10,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == expected.stdout


def test_nested_factory_parameters_locals_and_shadowing(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "factory_cells.py"
    source.write_text('''
import gc
def first(value: int, scale: int = 1) -> int:
    return (value + 1) * scale
def second(value: int, scale: int = 1) -> int:
    return (value + 2) * scale
def factory():
    def decorate(fn):
        cache = {}
        def wrapper(*args, **kwargs):
            key = (args, tuple(kwargs.items()))
            if key not in cache:
                cache[key] = fn(*args, **kwargs)
            return cache[key]
        return wrapper
    return decorate
def shadowed():
    value = 100
    def middle(value):
        def read():
            return value
        def write(new):
            nonlocal value
            value = new
        return read, write
    def outer_value():
        return value
    return middle, outer_value
def grandparent():
    value = 1
    def middle():
        def step():
            nonlocal value
            value += 1
            return value
        return step
    return middle()
def attributed(value: int):
    def inner():
        return value
    inner.label = value
    print(inner is inner)
    return inner
def mapping_factory(key, value):
    def read():
        return {key: (value, {key: value})}
    return read
def main():
    decorate = factory()
    left = decorate(first)
    right = decorate(second)
    gc.collect()
    print(left(4), right(4), left(4, scale=2), left(4))
    middle, outer_value = shadowed()
    read_a, write_a = middle(3)
    read_b, write_b = middle(9)
    write_a(7)
    gc.collect()
    print(read_a(), read_b(), outer_value())
    write_b(11)
    print(read_a(), read_b(), outer_value())
    step = grandparent()
    print(step(), step())
    one = attributed(3)
    two = attributed(8)
    gc.collect()
    print(one.label, two.label, one(), two(), one is two)
    mapping = mapping_factory("captured", 17)
    print(mapping())
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "factory_cells"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=20, env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"
