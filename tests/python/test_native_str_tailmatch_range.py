"""Native prefix/suffix windows use codepoint indices and preserve callbacks."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.py_frontend.pipeline import compile_python


@pytest.mark.parametrize("annotation", [": str", ""], ids=["typed", "dynamic"])
def test_tailmatch_windows(tmp_path, pcc_py_runtime_archive, annotation):
    source = tmp_path / "tailmatch.py"
    source.write_text('''
import gc
events = []
class Bound:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        events.append("index")
        gc.collect()
        return self.value
def bound(value):
    events.append("argument")
    return Bound(value)
def check(textANNOTATION):
    print(text.startswith("é", 1), text.endswith("é", 0, 2))
    print(text.startswith(("no", "é"), -3), text.endswith(("no", "llo"), 0))
    print(text.startswith("", 4), text.startswith("", 5), text.endswith("", 5))
    print(text.startswith("", 2, 1), text.endswith("", 2, 1))
    print(text.startswith("h", None, None), text.endswith("llo", None, None))
    print(text.startswith("", 10 ** 100), text.endswith("o", -(10 ** 100), 10 ** 100))
    print(text.startswith("é", bound(1), bound(2)))
    print(events)
    for prefix in [42, b"h", (42,), (("h",),)]:
        try:
            print(text.startswith(prefix, 0))
        except TypeError:
            print("bad prefix")
    print(text.startswith(("h", 42), 0))
    try:
        print(text.endswith("o", 1.5))
    except TypeError:
        print("bad bound")
def main():
    check("héllo")
    print("a/*x*/b".startswith("/*", 1))
main()
'''.replace("ANNOTATION", annotation), encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "tailmatch"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"


def test_tailmatch_argument_lifetime(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "tailmatch_ownership.py"
    source.write_text('''
import gc
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
prefix = "x" * 257
def rebind():
    global prefix
    prefix = "changed"
    gc.collect()
    return 0
def failed():
    raise ValueError("expected")
def scan(text: str, count: int):
    for index in range(count):
        try:
            text.startswith("x" * 257, failed())
        except ValueError:
            pass
def main():
    text = "x" * 258
    print(text.startswith(prefix, rebind()))
    scan(text, 8)
    before = live_bytes()
    scan(text, 2000)
    print(live_bytes() - before)
main()
''', encoding="utf-8")
    output = tmp_path / "tailmatch_ownership"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        lines = result.stdout.splitlines()
        assert lines[0] == "True", f"GC{backend}: {result.stdout}"
        if backend == 0:
            assert int(lines[1]) < 65536, result.stdout


def test_native_preprocessor_comment_scanning(tmp_path, pcc_py_runtime_archive):
    from pcc.preprocessor import _source_lines
    from pcc.py_frontend.pipeline import compile_python_multi

    text = 'int/**/value = 1; // trailing\nchar *s = "/* literal */";\n'
    source = tmp_path / "preprocessor_scan.py"
    source.write_text(
        "from pcc.preprocessor import _source_lines\n"
        "def main():\n    print(_source_lines(" + repr(text) + "))\nmain()\n",
        encoding="utf-8",
    )
    output = tmp_path / "preprocessor_scan"
    provider = Path(__file__).resolve().parents[2] / "pcc/preprocessor.py"
    compile_python_multi(
        [str(provider), str(source)], str(output),
        module_names=["pcc.preprocessor", "preprocessor_scan"],
        entry_module="preprocessor_scan", recursive_stdlib=True,
        backend="self", libpython_mode="off",
        runtime_archive=str(pcc_py_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == str(_source_lines(text)) + "\n"
