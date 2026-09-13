"""Context managers stay alive across yield and exit on resume/close/throw."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_generator_with_context_lifetime_and_cleanup(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'generator_context.py'
    source.write_text('''
import os
import gc
import tempfile
events = []
class Manager:
    def __enter__(self):
        events.append("enter")
        return 7
    def __exit__(self, et, ev, tb):
        events.append("exit")
        return False

def temporary(other):
    with tempfile.TemporaryDirectory(prefix="pcc_gen_ctx_") as path:
        yield path
        path = other
        yield "again"
        return 42

def ordinary():
    with Manager(), Manager() as value:
        yield value
    yield 8

def main():
    with tempfile.TemporaryDirectory(prefix="pcc_gen_other_") as other:
        iterator = temporary(other)
        path = next(iterator)
        gc.collect()
        print(os.path.isdir(path), next(iterator))
        try:
            next(iterator)
        except StopIteration as exc:
            print(exc.value)
        print(os.path.exists(path), os.path.isdir(other))
        iterator = temporary(other)
        path = next(iterator)
        iterator.close()
        print(os.path.exists(path))
        iterator = temporary(other)
        path = next(iterator)
        try:
            iterator.throw(ValueError("stop"))
        except ValueError:
            print(os.path.exists(path))
    iterator = ordinary()
    print(next(iterator))
    gc.collect()
    print(next(iterator), events)
    iterator.close()
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'generator_context'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
