"""A script filename is data, not an unescaped backend symbol suffix."""

import subprocess

import pytest


PROGRAMS = [
    "def answer():\n    return 42\nprint(answer())\n",
    '''
class Counter:
    def __init__(self):
        self.base = 40
    def add(self, value):
        return self.base + value
    @staticmethod
    def inc(value):
        return value + 1
def make():
    counter = Counter()
    callback = counter.add
    def apply(value):
        return callback(value)
    return apply
def answer():
    callback = make()
    step = lambda value: Counter.inc(value)
    return callback(step(1))
print(answer())
''',
]


@pytest.mark.parametrize("filename", ["program-name.py", "program name.py"])
@pytest.mark.parametrize("program", PROGRAMS, ids=["function", "adapters"])
def test_script_filename_punctuation_does_not_break_import_publication(
    tmp_path, pcc_runtime_archive, python_program_compiler, filename, program,
):
    source = tmp_path / filename
    source.write_text(program)
    output = tmp_path / "program"
    python_program_compiler(str(source), str(output), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "42\n"
