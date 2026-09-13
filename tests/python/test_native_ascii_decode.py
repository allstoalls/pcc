"""ASCII decode and decoding failures preserve ordinary call evaluation."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_ascii_decode_and_immediate_errors(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "ascii_decode.py"
    source.write_text('''
import gc
data = b"kept"
events = []
def encoding():
    global data
    data = b"rebound"
    gc.collect()
    events.append("encoding")
    return "ascii"
def errors():
    events.append("errors")
    return "strict"
def dynamic(value, name):
    return value.decode(name)
class Decoder:
    def decode(self, name):
        return "custom " + name
def main():
    print(int(b"  42  ".decode("ascii").strip()))
    print(bytearray(b"value").decode("US-ASCII"))
    print(dynamic(b"dynamic", "us_ascii"))
    print(dynamic(Decoder(), "ascii"))
    print(str(b"text", "ascii"))
    print(data.decode(errors=errors(), encoding=encoding()))
    print(events)
    try:
        b"value".decode("unregistered-encoding")
        print("unreachable")
    except LookupError:
        print("method error")
    try:
        dynamic(b"value", "unregistered-encoding")
        print("unreachable")
    except LookupError:
        print("dynamic error")
    try:
        str(b"value", "unregistered-encoding")
        print("unreachable")
    except LookupError:
        print("constructor error")
    try:
        b"value".decode("ascii", None)
    except TypeError:
        print("errors type")
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "ascii_decode"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"
