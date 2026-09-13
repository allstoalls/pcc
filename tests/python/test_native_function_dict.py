"""Function attributes expose their actual mutable dictionary."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_function_dictionary_alias_mutation_and_replacement(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "function_dict.py"
    source.write_text('''
import gc
def make():
    def function():
        return 42
    return function
def main():
    function = make()
    attributes = function.__dict__
    print(attributes is function.__dict__, len(attributes))
    attributes["note"] = [1, 2]
    gc.collect()
    print(function.note)
    function.__dict__ = {"flag": True}
    gc.collect()
    print(function.flag, "note" in function.__dict__, attributes["note"])
    try:
        function.__dict__ = 3
    except TypeError:
        print("type error")
    print(function())
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "function_dict"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"
