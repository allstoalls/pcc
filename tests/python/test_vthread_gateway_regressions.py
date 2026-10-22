"""Generic compiler regressions found by the external dashboard package."""

from pathlib import Path
import subprocess

import pytest

from pcc.frontends.python.codegen.vthread_effect_analysis import (
    classify_vthread_park_boundaries,
    compute_vthread_may_park_functions,
)
from pcc.frontends.python.parser import parse


def test_concrete_nonparking_method_does_not_inherit_another_class_effect():
    module = parse('''import pcc.virtual_thread as vt
class Parking:
    def close(self):
        vt.yield_now()
class Resource:
    def close(self):
        return 42
def worker():
    resource = Resource()
    vt.yield_now()
    return resource.close()
''', "concrete_nonparking.py")
    _, names = compute_vthread_may_park_functions(module)
    rejected = classify_vthread_park_boundaries(module, names)
    assert "worker" not in rejected, rejected


def test_unknown_receiver_with_effectful_method_name_is_still_rejected():
    module = parse('''import pcc.virtual_thread as vt
class Parking:
    def close(self):
        vt.yield_now()
def worker(resource):
    vt.yield_now()
    resource.close()
''', "unknown_receiver.py")
    _, names = compute_vthread_may_park_functions(module)
    assert "worker" in classify_vthread_park_boundaries(module, names)


def test_parking_finally_preserves_the_return_value(tmp_path: Path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "parking_finally.py"
    executable = tmp_path / "parking_finally"
    source.write_text('''import pcc.virtual_thread as vt
def cleanup():
    vt.yield_now()
def worker():
    try:
        return 42
    finally:
        cleanup()
thread = vt.spawn(worker)
vt.run(1, 64)
print(vt.result(thread))
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(executable)], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "42"


@pytest.mark.parametrize("binding", (" as original", ""))
def test_parked_handler_preserves_implicit_exception_context(tmp_path: Path, binding):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "parked_context.py"
    executable = tmp_path / "parked_context"
    source.write_text('''import pcc.virtual_thread as vt
def failing():
    try:
        raise ValueError("original")
    except ValueError''' + binding + ''':
        vt.yield_now()
        raise RuntimeError("replacement")
def worker():
    try:
        failing()
    except RuntimeError as error:
        print(str(error.__context__))
        return str(error)
thread = vt.spawn(worker)
vt.run(1, 64)
print(vt.result(thread))
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(executable)], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == ["original", "replacement"]


def test_parked_conditional_argument_keeps_unambiguous_roots(tmp_path: Path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "conditional_argument.py"
    executable = tmp_path / "conditional_argument"
    (tmp_path / "response_model.py").write_text('''class Response:
    def __init__(self, status: int = 200, body=b"", headers=None, streaming: bool = False) -> None:
        if headers is None:
            headers = []
        self.status = status
        self.body = body
        self.headers = list(headers)
        self.streaming = streaming
    @classmethod
    def text(cls, body, status: int = 200):
        return cls(status, body)
''', encoding="utf-8")
    source.write_text('''import pcc.virtual_thread as vt
import threading
from pcc.unsafe import null
from response_model import Response
def code():
    return 200, "", False
def worker():
    status = 0
    attempt = 0
    while attempt < 2:
        status, failure, committed = vt.call(code)
        attempt += 1
    response = Response(status if status else 502, b"")
    vt.yield_now()
    return status + response.status
thread = vt.spawn(worker)
vt.run(1, 64)
print(vt.result(thread))
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(executable)], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "400"


def test_single_carrier_run_returns_control_for_pending_timers(tmp_path: Path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "sleeping_child.py"
    executable = tmp_path / "sleeping_child"
    source.write_text('''import pcc.virtual_thread as vt
def child():
    vt.sleep_current(20)
    return 42
def main():
    thread = vt.spawn(child)
    vt.run(1, 64)
    print(vt.outcome(thread))
    print(vt.result(thread))
main()
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(executable)], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == ["0", "None"]


def test_typed_lock_field_uses_native_threading_lowering(tmp_path: Path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "lock_field.py"
    executable = tmp_path / "lock_field"
    source.write_text('''from threading import Lock
import pcc.virtual_thread as vt
class Counter:
    def __init__(self):
        self.lock: Lock = Lock()
        self.value = 0
    def increment(self):
        vt.yield_now()
        self.lock.acquire()
        try:
            self.value += 1
            return self.value
        finally:
            self.lock.release()
def worker():
    return Counter().increment()
thread = vt.spawn(worker)
vt.run(1, 64)
print(vt.result(thread))
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(executable)], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "1"


@pytest.mark.parametrize("cleanup", (False, True))
def test_generator_owned_local_return_survives_cleanup(tmp_path: Path, cleanup):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "owned_return.py"
    executable = tmp_path / "owned_return"
    ending = (
        "    try:\n        return values\n"
        "    finally:\n        values = []\n        vt.yield_now()\n"
        if cleanup else "    return values\n"
    )
    source.write_text('''import pcc.virtual_thread as vt
def child():
    vt.yield_now()
    values = [1, 2]
''' + ending + '''
def main():
    index = 0
    while index < 3:
        thread = vt.spawn(child)
        vt.run(1, 64)
        result = vt.result(thread)
        print(result)
        index += 1
main()
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(executable)], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == ["[1, 2]"] * 3


def test_generator_walk_covers_expressions_without_dataclass_reflection(monkeypatch):
    from pcc.frontends.python.codegen.generator_lowering import _dataclass_field_names
    from pcc.frontends.python.py_ast import Expr, UnaryOp

    module = parse("def worker():\n    return not answer()\n", "unary.py")
    expression = module.body[0].body[0].value
    monkeypatch.delattr(UnaryOp, "__dataclass_fields__")
    monkeypatch.delattr(Expr, "__dataclass_fields__")
    assert "operand" in _dataclass_field_names(expression)


def test_parking_call_in_negated_condition(tmp_path: Path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "negated_call.py"
    executable = tmp_path / "negated_call"
    source.write_text('''import pcc.virtual_thread as vt
def yes():
    vt.yield_now()
    return True
def work():
    if not vt.call(yes):
        return 1
    return 42
thread = vt.spawn(work)
vt.run(1, 64)
print(vt.result(thread))
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "42"
