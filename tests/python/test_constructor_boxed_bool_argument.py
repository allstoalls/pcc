"""Dynamic attribute values must preserve bool constructor arguments."""
import os
from pathlib import Path
import subprocess

PROGRAM = '''from dataclasses import dataclass
@dataclass(frozen=True, slots=True)
class Input:
    pcrel: bool
@dataclass(frozen=True, slots=True)
class Output:
    pcrel: bool
    offset: int = 0
def convert(values):
    result = []
    for value in values:
        result.append(Output(pcrel=value.pcrel))
    return result
def main():
    values = convert((Input(True), Input(False)))
    print(values[0].pcrel, values[1].pcrel)
main()
'''

def test_dynamic_bool_attribute_survives_constructor_call(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / 'bool_argument.py'
    source.write_text(PROGRAM)
    binary = tmp_path / 'bool_argument'
    compile_python(str(source), str(binary), backend='self',libpython_mode='off',ir_scaffold_mode='on',runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)],env=dict(os.environ,PCC_GC_BACKEND=str(backend)),capture_output=True,text=True,timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'True False'
