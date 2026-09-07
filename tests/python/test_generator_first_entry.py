"""Generator arguments and live locals survive first entry and later resumes."""

import os
from pathlib import Path
import re
import subprocess

import pytest


@pytest.mark.parametrize("first_entry_init", ["0", "1"])
def test_first_entry_and_resumed_locals_preserve_protocol(
    tmp_path: Path, monkeypatch, pcc_py_runtime_archive, first_entry_init,
):
    from pcc.py_frontend.pipeline import compile_python

    monkeypatch.setenv("PCC_GENERATOR_FIRST_ENTRY_INIT", first_entry_init)
    source = tmp_path / "first_entry.py"
    executable = tmp_path / "first_entry"
    source.write_text('''import gc
from pcc.extern import c_int64, extern
backend_id = extern("pcc_gc_backend", (), c_int64)
print(backend_id())
events = []

def worker(seed):
    saved = seed
    try:
        incoming = yield saved
        saved.append(incoming)
        gc.collect()
        yield saved
    except ValueError as error:
        yield str(error)
    finally:
        events.append(saved[0])

first = worker([1])
second = worker([8])
gc.collect()
print(next(first))
print(next(second))
print(first.send(2))
print(second.send(9))
print(first.throw(ValueError("stop")))
first.close()
second.close()
gc.collect()
print(events)
''')
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    expected = ["[1]", "[8]", "[1, 2]", "[8, 9]", "stop", "[1, 8]"]
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        ran = subprocess.run([str(executable)], env=environment, text=True,
                             capture_output=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip().splitlines() == [str(backend), *expected], f"GC{backend}: " + ran.stdout


def test_first_entry_reads_arguments_without_reading_placeholder_slots(tmp_path, monkeypatch):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "entry_shape.py"
    source.write_text('''def worker(seed):
    first = seed
    second = seed
    yield first
    yield second
g = worker(5)
print(next(g))
''')
    counts = []
    for enabled in ("0", "1"):
        monkeypatch.setenv("PCC_GENERATOR_FIRST_ENTRY_INIT", enabled)
        output = tmp_path / ("entry_" + enabled + ".ll")
        compile_python(str(source), str(output), emit_llvm_only=True,
                       libpython_mode="off", ir_scaffold_mode="on", backend="self")
        ir = output.read_text()
        resume = re.search(r"define[^\n]*worker__gen_resume[^\n]*\{\n(.*?)\n\}", ir, re.S)
        assert resume, "the test must emit the actual generator resume body"
        entry = resume.group(1).split("\ngen.dispatch:", 1)[0]
        counts.append(len(re.findall(r"call[^\n]*@py_list_get(?:_for_frame)?\(", entry)))
        if enabled == "1":
            assert "gen.restore.locals:" in resume.group(1)
    assert counts == [3, 1], "only the argument needs restoration on first entry"
