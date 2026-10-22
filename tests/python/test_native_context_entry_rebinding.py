"""The enter result must survive disposal of the previous as-target owner."""
import os
import subprocess
import sys


PROGRAM = '''import builtins as io
import gc
released = 0
class Previous:
    def __del__(self):
        global released
        released += 1
        pressure = []
        for index in range(24):
            pressure.append(str(index) * 256)
        gc.collect()
def read_file(path):
    stream = Previous()
    with io.open(path, "rb") as stream:
        result = stream.read()
    assert stream.closed
    return result
def main():
    for index in range(32):
        assert read_file(PATH) == b"entry-owner"
        gc.collect()
    assert released == 32
    print("context-entry-owner-ok", released)
main()
'''


def test_context_enter_survives_previous_target_finalizer_on_every_gc(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"entry-owner")
    source = tmp_path / "context_entry.py"
    source.write_text(PROGRAM.replace("PATH", repr(str(payload))), encoding="utf-8")
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, timeout=20)
    assert oracle.returncode == 0, oracle.stderr
    binary = tmp_path / "context_entry"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == oracle.stdout, (backend, ran.stdout, oracle.stdout)
