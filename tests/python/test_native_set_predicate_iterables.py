"""Set predicates accept iterables and preserve iterator short-circuiting."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_set_predicate_iterables(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "set_predicates.py"
    source.write_text('''
import gc
events = []
class Key:
    def __hash__(self):
        gc.collect()
        events.append("hash")
        return 42
def values():
    events.append(1)
    gc.collect()
    yield 1
    events.append(2)
    yield 2
    events.append(3)
    raise ValueError("late")
def main():
    print({"a"}.issubset({"a": 1, "b": 2}))
    print(frozenset({"a", "b"}).issubset({"a": 1}))
    print({1, 2}.issubset([1, 1, 2]), {1, 2}.issuperset((1, 1)))
    print({1}.issubset(values()), events)
    events.clear()
    print({1}.issuperset(values()), events)
    events.clear()
    print(set().issuperset(values()), events)
    events.clear()
    try:
        print(set().issubset(values()))
    except ValueError:
        print("late error", events)
    for other in [42, [[1]]]:
        try:
            print({1}.issubset(other))
        except TypeError:
            print("bad subset input")
        try:
            print({1}.issuperset(other))
        except TypeError:
            print("bad superset input")
    print({1}.issubset({1, 2}), {1, 2}.issuperset({1}))
    key = Key()
    left = {key}
    right = {key}
    events.clear()
    print(left.issubset([key]), events)
    events.clear()
    print(left.issubset(right), events)
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "set_predicates"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"
