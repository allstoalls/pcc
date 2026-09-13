"""Native regex substitution invokes replacement callbacks with Match objects."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_callable_replacements_groups_counts_and_errors(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "sub_callback.py"
    source.write_text('''
import re
import gc
seen = []
def replace(match):
    gc.collect()
    seen.append(match)
    return "[" + match.group(1) + "]"
def bad(match):
    return 42
def failed(match):
    raise ValueError("replacement failed")
def main():
    print(re.sub("([a-z]+)", replace, "one 2 two"))
    print(seen[0].group(0), seen[1].group(1))
    print(re.sub("([a-z]+)", replace, "one two", 1))
    print(re.sub("(missing)", replace, "kept"))
    pattern = re.compile("([a-z]+)")
    print(pattern.sub(replace, "three"))
    try:
        re.sub("(x)", bad, "x")
    except TypeError:
        print("bad replacement")
    try:
        re.sub("(x)", failed, "x")
    except ValueError:
        print("callback exception")
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "sub_callback"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"
