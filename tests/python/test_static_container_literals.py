"""Static literal emitters share the fixed-layout L1CodeGen host state."""

import os
import subprocess
import sys


PROGRAM = '''
def tables():
    first = {"one": 1, "two": 2}
    second = {"three": 3, "four": 4}
    sequence = [5, 6]
    pair = (7, 8)
    nested = {"outer": [(9, 10), {"inner": 11}]}
    another = ([12, 13], {"last": 14})
    return first, second, sequence, pair, nested, another

def main():
    left = tables()
    right = tables()
    left[4]["outer"][1]["inner"] = 99
    left[5][0].append(15)
    print(left[0]["one"], right[1]["four"], left[2][1], right[3][0])
    print(left[4]["outer"][1]["inner"], right[4]["outer"][1]["inner"])
    print(len(left[5][0]), len(right[5][0]), right[5][1]["last"])
    try:
        raise ValueError("after literals")
    except ValueError as error:
        print(str(error))

main()
'''


def test_static_container_emission_preserves_values_and_fresh_nested_objects(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    monkeypatch.setenv("PCC_STATIC_AGGREGATE", "1")
    source = tmp_path / "static_containers.py"
    source.write_text(PROGRAM)
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "static_containers"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stdout}{result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"


def test_static_container_probe_reaches_each_counter(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_STATIC_AGGREGATE", "1")
    source = tmp_path / "static_containers.py"
    source.write_text(PROGRAM)
    output = tmp_path / "static_containers.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    text = output.read_text()
    for name in (".pcc.static.dict.1", ".pcc.static.dict.2", ".pcc.static.list.1",
                 ".pcc.static.tuple.2", ".pcc.static.agg.dict.1", ".pcc.static.agg.tuple.2"):
        assert "@" + name + " =" in text, name
    monkeypatch.setenv("PCC_STATIC_AGGREGATE", "off")
    plain_output = tmp_path / "plain_containers.ll"
    compile_python(str(source), str(plain_output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    assert "@.pcc.static.agg." not in plain_output.read_text()
