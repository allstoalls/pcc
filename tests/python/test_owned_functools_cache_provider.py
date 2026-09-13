"""Exercise the callable cache provider, without decorator-call shortcuts."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


SOURCE = '''from functools import lru_cache
calls = 0
def counted(*args, **kwargs):
    global calls
    calls += 1
    return calls
def main():
    global calls
    for limit in [0, 1, 2, None]:
        calls = 0
        decorate = lru_cache(maxsize=limit)
        cached = decorate(counted)
        print(cached(1), cached(2), cached(1), cached(3), cached(2))
        print(tuple(cached.cache_info()))
        print(cached.cache_parameters())
        cached.cache_clear()
        print(tuple(cached.cache_info()))
    for typed in [False, True]:
        calls = 0
        cached = lru_cache(maxsize=4, typed=typed)(counted)
        print(cached(1), cached(1.0), cached(True))
    calls = 0
    cached = lru_cache(maxsize=4)(counted)
    print(cached(a=1, b=2), cached(b=2, a=1), cached(a=1, b=2))
    try:
        cached([])
    except TypeError:
        print("unhashable")
    uncached = lru_cache(maxsize=0)(counted)
    print(uncached([]))
main()
'''


def test_owned_cache_provider_matches_cpython(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "cache_values.py"
    source.write_text(SOURCE, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "cache_values"
    compile_python(
        str(source), str(binary), backend="self",
        libpython_mode="off", runtime_archive=str(pcc_py_runtime_archive),
    )
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=20, env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"
