"""A walrus binding keeps its own reference after the truth test consumes a temporary."""
import os
from pathlib import Path
import subprocess
import pytest


@pytest.mark.parametrize("expression", ['re.match(r"(?P<word>[a-z]+)", "alpha")', 'pattern.match("alpha")', 'pattern.match((" alpha ").strip())'])
def test_walrus_match_survives_truth_test_and_collection(tmp_path: Path, pcc_py_runtime_archive, expression):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "walrus_match.py"
    source.write_text('''import re
import gc
from pcc.extern import extern, c_int64
pattern = re.compile(r"(?P<word>[a-z]+)")

def exercise():
    if match := MATCH_EXPRESSION:
        gc.collect()
        print(match.group("word"))
    else:
        raise RuntimeError("match lost")
exercise()
'''.replace("MATCH_EXPRESSION", expression))
    binary = tmp_path / "walrus_match"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "alpha"


def test_walrus_borrowed_aliases_rebinding_and_truth_failure(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "walrus_aliases.py"
    source.write_text('''import gc
released = []
class Probe:
    def __init__(self, label: str):
        self.label = label
    def __bool__(self):
        gc.collect()
        if self.label == "throws":
            raise ValueError("truth failed")
        return False
    def __del__(self):
        released.append(self.label)
def exercise(value):
    if value := Probe("false"):
        raise RuntimeError("wrong truth value")
    if value.label != "false":
        raise RuntimeError("parameter binding lost")
    anchor = [7]
    if borrowed := anchor:
        anchor = []
        gc.collect()
        if borrowed[0] != 7:
            raise RuntimeError("borrowed value lost")
    alias = (other := [8])
    other = []
    gc.collect()
    if alias[0] != 8:
        raise RuntimeError("expression result lost")
    left = right = [9]
    left = []
    gc.collect()
    if right[0] != 9:
        raise RuntimeError("chained binding lost")
    try:
        if bad := Probe("throws"):
            pass
    except ValueError:
        pass
    if bad.label != "throws":
        raise RuntimeError("raising truth test lost binding")
def main():
    exercise([11])
    gc.collect()
    print(sorted(released))
main()
''')
    binary = tmp_path / "walrus_aliases"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "['false', 'throws']"
