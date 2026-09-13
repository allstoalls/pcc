"""RLock recursion and context exits must leave the mutex unowned."""

import os
import subprocess

from pcc.py_frontend.pipeline import compile_python


def test_rlock_recursion_and_conditional_context_exit(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "rlock_scope.py"
    source.write_text('''
from threading import RLock
from pcc.extern import c_obj, c_int64, extern
release_status = extern("py_threading_rlock_release", (c_obj,), c_int64)
lock = RLock()
def nested():
    with lock:
        with lock:
            return 42
def branch(flag: bool):
    with lock:
        if flag:
            return 10
    return 20
def main():
    print(nested(), release_status(lock))
    print(branch(False), release_status(lock))
    print(branch(True), release_status(lock))
main()
''', encoding="utf-8")
    binary = tmp_path / "rlock_scope"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == "42 -1\n20 -1\n10 -1\n", f"GC{backend}: {result.stdout}"
