"""Awaited temporaries and yielded requests release their owners on completion."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_await_releases_temporary_coroutines_and_requests(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "await_owners.py"
    source.write_text('''import gc
released = []
class Token:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        released.append(self.label)
class Pause:
    def __init__(self, token):
        self.token = token
    def __await__(self):
        yield self
        return self.token.label
async def inner(token):
    return token.label
async def immediate():
    return await inner(Token("immediate"))
async def suspended():
    return await Pause(Token("suspended"))
def drive(coro, suspend):
    if suspend:
        yielded = coro.send(None)
        if yielded.token.label != "suspended":
            raise RuntimeError("bad suspension")
        gc.collect()
    try:
        coro.send(None)
    except StopIteration as stopped:
        print(stopped.value)
def exercise():
    drive(immediate(), False)
    drive(suspended(), True)
def main():
    exercise()
    gc.collect()
    print(sorted(released))
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "await_owners"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stdout}; {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"
