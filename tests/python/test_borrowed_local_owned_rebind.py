"""Replacing a borrowed alias must not consume the caller's reference."""

import os
import subprocess
import sys


SOURCE = '''def replace_borrowed(text: str) -> str:
    current = text
    replacement = text.replace("a", "b")
    current = replacement
    return current

def main():
    original = "a" * 200000
    result = replace_borrowed(original)
    print(len(original), len(result), original[0], result[0])

main()
'''


def test_owned_rebind_preserves_borrowed_source_on_all_gc_backends(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "borrowed_rebind.py"
    source.write_text(SOURCE)
    expected = subprocess.check_output([sys.executable, str(source)], text=True, timeout=10)
    binary = tmp_path / "borrowed_rebind"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected


def test_loop_borrowed_rebind_clears_previous_iteration_owner(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "borrowed_loop.py"
    source.write_text('''
def chunks(source: str) -> list[str]:
    result = []
    for offset in range(4):
        current = source
        if offset:
            current = "x16"
        result.append(current)
    return result
def main():
    for index in range(100):
        value = "x" + str(index)
        result = chunks(value)
        assert result[0] == value
        assert result[1] == "x16"
    print(value, result[0], len(result))
main()
''')
    binary = tmp_path / "borrowed_loop"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                     PCC_GC_REFCOUNT_PROVENANCE_PROBE="3", PCC_GC_KNOWN_REF_CHECKS="1"))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "x99 x99 4\n"


def test_loop_borrowed_rebind_releases_previous_owned_value_once(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "loop_finalizers.py"
    source.write_text('''
import gc
destroyed = 0
class Canary:
    def __init__(self, value):
        self.value = value
    def __del__(self):
        global destroyed
        destroyed += 1
def replace_in_loop(source):
    for index in range(4):
        current = source
        if index:
            current = Canary(index)
    return source.value
def main():
    original = Canary(42)
    print(replace_in_loop(original))
    gc.collect()
    print(destroyed, original.value)
    original = None
    gc.collect()
    print(destroyed)
main()
''')
    binary = tmp_path / "loop_finalizers"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "42\n3 42\n4\n", (backend, result.stdout)
