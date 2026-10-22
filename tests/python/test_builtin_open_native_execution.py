"""Execute qualified open, owner handoff and context unwinding on every GC."""
import os
import subprocess
import sys


def test_builtin_open_binding_and_context_lifetime_on_every_gc(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"owned-data")
    source = tmp_path / "open_lifetime.py"
    source.write_text('''import builtins as io
import gc
seen = []
events = []
def path_value():
    events.append("path")
    gc.collect()
    return PATH + ""
def mode_value():
    events.append("mode")
    gc.collect()
    return "r" + "b"
def bad_mode():
    events.append("bad-mode")
    raise ValueError("mode")
def read_return(open):
    with io.open(path_value(), mode_value()) as stream:
        seen.append(stream)
        return stream.read()
def body_error():
    try:
        with io.open(PATH, "rb") as stream:
            seen.append(stream)
            raise ValueError("body")
    except ValueError:
        return stream.closed
def rebound_target():
    with io.open(PATH, "rb") as stream:
        seen.append(stream)
        stream = None
        return 7
def main():
    for index in range(16):
        assert read_return(None) == b"owned-data"
        assert seen[-1].closed
        assert body_error()
        assert rebound_target() == 7
        assert seen[-1].closed
        try:
            io.open(path_value(), bad_mode())
        except ValueError:
            pass
        gc.collect()
    assert len(seen) == 48
    assert events[:4] == ["path", "mode", "path", "bad-mode"]
    print("open-owner-ok", len(seen), len(events))
main()
'''.replace("PATH", repr(str(payload))), encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=20)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "open_lifetime"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == expected.stdout, (backend, ran.stdout, expected.stdout)
