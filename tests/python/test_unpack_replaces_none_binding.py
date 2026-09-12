"""Unpacking unknown sequences replaces earlier None bindings."""

import os
import subprocess

from pcc.py_frontend.pipeline import compile_python


def test_unpack_replaces_none_before_string_operations(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "unpack.py"
    source.write_text('''
def pair(values: object):
    left = None
    right = None
    left, right = values
    print(left.strip(), right.strip())
def goal(spec: str):
    name = spec
    target = None
    if "=" in spec:
        name, target = spec.rsplit("=", 1)
    target = target.strip() or None if target is not None else None
    print(name.strip(), target)
def main():
    pair([" left ", " right "])
    pair((" one ", " two "))
    goal("path")
    goal(" path = target ")
    goal("path= ")
main()
''', encoding="utf-8")
    output = tmp_path / "unpack"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == "left right\none two\npath None\npath target\npath None\n"
