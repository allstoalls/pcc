"""dict.get must consume its eagerly evaluated default even on a hit."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("key", ["hit", "miss"])
def test_dict_get_releases_fresh_defaults(tmp_path: Path, pcc_py_runtime_archive, key):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "dict_default.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(count: int) -> int:
    values = {"hit": [7]}
    index = 0
    total = 0
    while index < count:
        result = values.get(KEY, [])
        total += len(result)
        index += 1
    return total
def main():
    scan(16)
    before = live_bytes()
    first = scan(2000)
    middle = live_bytes()
    second = scan(2000)
    print(first, second, middle - before, live_bytes() - middle)
main()
'''.replace("KEY", repr(key)))
    binary = tmp_path / "dict_default"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == (2000 if key == "hit" else 0)
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_dict_get_preserves_aliases_rebinding_and_failure_cleanup(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "dict_get_lifetime.py"
    source.write_text('''import gc
anchor = {"hit": [7]}
released = []
class Key:
    def __hash__(self):
        gc.collect()
        raise ValueError("bad hash")
    def __del__(self):
        released.append("key")
class Fallback:
    def __del__(self):
        released.append("default")
def rebind() -> str:
    global anchor
    anchor = {}
    gc.collect()
    return "hit"
def fail():
    raise ValueError("bad default")
def exercise():
    result = anchor.get(rebind(), [])
    if result[0] != 7:
        raise RuntimeError("receiver replaced before lookup")
    values = {}
    fallback = []
    if values.get("missing", fallback) is not fallback:
        raise RuntimeError("default identity changed")
    try:
        values.get(Key(), Fallback())
    except ValueError:
        pass
    try:
        values.get(Key(), fail())
    except ValueError:
        pass
def main():
    exercise()
    gc.collect()
    print(sorted(released))
main()
''')
    binary = tmp_path / "dict_get_lifetime"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "['default', 'key', 'key']"
