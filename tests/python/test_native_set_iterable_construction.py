"""Set construction and update consume the iterator protocol for unknown inputs."""
import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_set_construction_from_dynamic_iterables(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "set_inputs.py"
    source.write_text('''import gc
destroyed = []
class Key:
    def __init__(self, value):
        self.value = value
    def __hash__(self):
        gc.collect()
        return self.value
    def __eq__(self, other):
        gc.collect()
        return self.value == other.value
    def __del__(self):
        destroyed.append(self.value)
def key_items(fail):
    yield Key(1)
    if fail:
        raise ValueError("after item")
    yield Key(2)
    yield Key(1)
def keep_keys():
    keys = set(key_items(False))
    gc.collect()
    print(sorted([key.value for key in keys]))
def lifetime_checks():
    keep_keys()
    gc.collect()
    print("released", sorted(destroyed))
    destroyed.clear()
    try:
        set(key_items(True))
    except ValueError:
        pass
    gc.collect()
    print("failed released", sorted(destroyed))
def collect(values: object):
    print(sorted(set(values)))
def frozen(values: object):
    print(sorted(frozenset(values)))
def spread(values: object):
    print(sorted({*values}))
def update(values: object):
    result = set()
    result.update(values)
    print(sorted(result))
def items():
    yield 3
    yield 1
    yield 3
def broken_items():
    yield 1
    raise ValueError("iterator failure")
class HashFailure:
    def __hash__(self):
        raise StopIteration("hash failure")
def main():
    for values in [{"Scope": 1, "Other": 2}, [3, 1, 3], (4, 2, 4), "aba", range(3)]:
        collect(values)
        frozen(values)
        spread(values)
        update(values)
    collect(items())
    frozen(items())
    spread(items())
    update(items())
    for value in [None, 42]:
        try:
            collect(value)
        except TypeError:
            print("not iterable")
    partial = set()
    try:
        partial.update(broken_items())
    except ValueError:
        print("partial", sorted(partial))
    try:
        collect([HashFailure()])
    except StopIteration:
        print("hash error preserved")
    lifetime_checks()
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "set_inputs"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}; {result.stdout}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"
