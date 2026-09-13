"""The public Python compilation API defaults to owned native emission."""
import ctypes
from pathlib import Path
import subprocess

from pcc.py_frontend.pipeline import compile_python


def test_default_pipeline_executes_without_external_codegen(tmp_path, monkeypatch, pcc_py_runtime_archive):
    source = tmp_path / 'api_default.py'
    source.write_text('''from functools import lru_cache
def increment(value):
    return value + 1
def main():
    cached = lru_cache(maxsize=2)(increment)
    print(cached(41), cached(41), tuple(cached.cache_info()))
main()
''', encoding='utf-8')
    binary = tmp_path / 'api_default'
    original_popen = subprocess.Popen
    original_cdll = ctypes.CDLL

    def owned_process(args, *positional, **kwargs):
        command = args[0] if not isinstance(args, str) else args.split()[0]
        name = Path(command).name
        assert name not in {'cc', 'clang', 'gcc', 'ar', 'ld', 'codesign', 'lipo'}, args
        assert not name.startswith(('llvm-', 'clang-', 'gcc-')), args
        return original_popen(args, *positional, **kwargs)

    def owned_library(name, *args, **kwargs):
        assert 'llvm' not in str(name).lower(), name
        return original_cdll(name, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.delenv('PCC_BACKEND', raising=False)
        scoped.delenv('PCC_PYTHON_LIBPYTHON', raising=False)
        scoped.setattr(subprocess, 'Popen', owned_process)
        scoped.setattr(ctypes, 'CDLL', owned_library)
        compile_python(str(source), str(binary), runtime_archive=str(pcc_py_runtime_archive))
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '42 42 (1, 1, 2, 1)\n'
