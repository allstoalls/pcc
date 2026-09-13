"""A class name shared with threading does not establish a native owner."""

import os
import subprocess

import pytest

from pcc.py_frontend.codegen.native_threading import NativeThreadingLoweringMixin
from pcc.py_frontend.py_ast import ClassType
from pcc.py_frontend.pipeline import compile_python, compile_python_multi


@pytest.mark.parametrize("name", ["Event", "Condition", "Semaphore", "Lock"])
def test_threading_method_type_requires_matching_module(name):
    host = NativeThreadingLoweringMixin()
    assert host._threading_kind_for_type(ClassType(name=name, module="other")) is None
    assert host._threading_kind_for_type(ClassType(name=name, module="threading")) == name


def test_imported_event_wait_does_not_turn_main_into_a_generator(
    tmp_path, pcc_py_runtime_archive,
):
    provider = tmp_path / "events.py"
    provider.write_text("class Event:\n    def wait(self):\n        return 42\n")
    source = tmp_path / "program.py"
    source.write_text(
        "from external_events import Event\n"
        "def main():\n    event = Event()\n    print(event.wait())\nmain()\n"
    )
    binary = tmp_path / "program"
    compile_python_multi(
        [str(provider), str(source)], str(binary),
        module_names=["external_events", "program"], entry_module="program",
        recursive_stdlib=True, backend="self", libpython_mode="off",
        runtime_archive=str(pcc_py_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == "42\n", f"GC{backend}: {result.stdout}"


def test_native_threading_event_still_parks_and_resumes(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "native_event.py"
    source.write_text('''
import threading
import pcc.virtual_thread as vt
event = threading.Event()
def waiter():
    event.wait()
    return 42
thread = vt.spawn(waiter)
vt.run(1, 64)
print(vt.outcome(thread))
event.set()
vt.run(1, 64)
print(vt.result(thread))
''', encoding="utf-8")
    binary = tmp_path / "native_event"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == "0\n42\n", f"GC{backend}: {result.stdout}"
