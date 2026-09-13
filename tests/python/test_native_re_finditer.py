"""Pattern.finditer is a native lazy iterator retaining match state."""

import os
from pathlib import Path
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_pattern_finditer(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "finditer.py"
    source.write_text('''
import re
import gc
class Bound:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        gc.collect()
        return self.value
def main():
    pattern = re.compile("([a-z]+)")
    iterator = pattern.finditer("one 2 two")
    print(iter(iterator) is iterator)
    first = next(iterator)
    gc.collect()
    second = next(iterator)
    print(first.group(1), first.start(), first.end())
    print(second.group(0), second.span())
    print(next(iterator, None), next(iterator, None))
    for match in pattern.finditer("one 2 two", 4, 8):
        gc.collect()
        print(match.group(0), match.start(), match.end())
    print(list(pattern.finditer("123")))
    print(list(pattern.finditer("word", 5)))
    print(list(pattern.finditer("word", 0, -1)))
    for match in re.finditer("([a-z]+)", "ONE 2 TWO", re.IGNORECASE):
        print(match.group(1), match.start())
    for match in re.finditer("%([a-z]+)", "%left + %right"):
        print(match.group(1))
    for match in pattern.finditer("one two", Bound(4), Bound(7)):
        print(match.group(1))
    for huge in [10 ** 100, -(10 ** 100), Bound(10 ** 100)]:
        try:
            pattern.finditer("word", huge)
        except OverflowError:
            print("overflowing bound")
    for match in pattern.finditer("one\\x00two"):
        print(match.group(0), match.start())
    for bad in [None, 1.5]:
        try:
            pattern.finditer("word", bad)
        except TypeError:
            print("bad bound")
    try:
        pattern.finditer("word", 0, 1, 2)
    except TypeError:
        print("too many arguments")
    try:
        pattern.finditer(42)
    except TypeError:
        print("bad text")
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "finditer"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"


def test_native_preprocessor_macro_scan(tmp_path, pcc_py_runtime_archive):
    from pcc.preprocessor import preprocess
    from pcc.py_frontend.pipeline import compile_python_multi

    text = "#define N 42\n#define INC(x) ((x)+1)\nint f(void) { return INC(N); }\n"
    expected = preprocess(text, base_dir=str(tmp_path))
    source = tmp_path / "macro_scan.py"
    source.write_text(
        "from pcc.preprocessor import preprocess\n"
        "def main():\n    print(preprocess(" + repr(text) + ", base_dir="
        + repr(str(tmp_path)) + "))\nmain()\n", encoding="utf-8",
    )
    output = tmp_path / "macro_scan"
    provider = Path(__file__).resolve().parents[2] / "pcc/preprocessor.py"
    compile_python_multi(
        [str(provider), str(source)], str(output),
        module_names=["pcc.preprocessor", "macro_scan"], entry_module="macro_scan",
        recursive_stdlib=True, backend="self", libpython_mode="off",
        runtime_archive=str(pcc_py_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected + "\n", f"GC{backend}: {result.stdout}"
