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


def test_owned_rebind_preserves_borrowed_source_on_all_gc_backends(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "borrowed_rebind.py"
    source.write_text(SOURCE)
    expected = subprocess.check_output([sys.executable, str(source)], text=True, timeout=10)
    binary = tmp_path / "borrowed_rebind"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected
