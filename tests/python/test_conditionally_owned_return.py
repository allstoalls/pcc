"""A local owned on one path still needs a retain on its borrowed return path."""

import os
import subprocess
import sys


_SOURCE = '''
def resolve(text: str, replacements: dict[str, str]) -> str:
    current = text
    while current in replacements:
        current = replacements[current]
    return current


def main():
    original = "a" * 200000
    selected = resolve(original, {})
    print(len(selected), selected[0])
    selected = "replacement" * 20000
    print(len(original), original[0], len(selected))
    replacement = "b" * 200000
    mapping = {original: replacement}
    selected = resolve(original, mapping)
    print(len(selected), selected[0])
    selected = "changed" * 30000
    print(len(original), original[0], len(replacement), replacement[0])

main()
'''


def test_conditionally_owned_return_keeps_caller_source_alive(tmp_path, pcc_diagnostic_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "owned_return.py"
    source.write_text(_SOURCE)
    reference = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert reference.returncode == 0, reference.stderr
    binary = tmp_path / "owned_return"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == reference.stdout, backend


def test_conditional_return_transfers_exactly_one_owner(tmp_path, pcc_diagnostic_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "return_finalizers.py"
    source.write_text('''
DELS = [0]
class Canary:
    def __init__(self, tag: int):
        self.tag = tag
    def __del__(self):
        DELS[0] += 1

def choose(source: Canary, replace: bool) -> Canary:
    current = source
    if replace:
        current = Canary(2)
    return current

def scope(replace: bool):
    original = Canary(1)
    result = choose(original, replace)
    print(original.tag, result.tag)

scope(False)
print(DELS[0])
scope(True)
print(DELS[0])
''')
    reference = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == "1 1\n1\n1 2\n3\n"
    binary = tmp_path / "return_finalizers"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == reference.stdout, backend
