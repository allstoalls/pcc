"""Iterable min/max must preserve float elements after dynamic container calls."""

import subprocess
import sys


def test_float_extrema_survive_dynamic_list_and_tuple_results(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "float_extrema.py"
    executable = tmp_path / "float_extrema"
    source.write_text('''def indirect(values):
    result = {"samples": values}
    return result.pop("samples")

def main():
    values = indirect([103.75, 101.25, 102.5])
    print(min(values))
    print(max(values))
    print(min(values) + 0.5)
    print(max(values) is values[0])
    print(min([3.75, -2.5, 1.25]))
    print(max((3.75, -2.5, 1.25)))
    print(min(indirect([2, 1.25, 5])))
    print(max(indirect([2, 1.25, 5])))
main()
''')
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, timeout=10, check=True)
    compile_python(str(source), str(executable), backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   runtime_archive=str(pcc_runtime_archive))
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == oracle.stdout
