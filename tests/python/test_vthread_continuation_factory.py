"""Factory bodies avoid implicit frames while calls retain parking semantics."""

import os
import subprocess
import re

import pytest


def test_factory_method_emits_without_a_generator_wrapper(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "factory_shape.py"
    source.write_text('''import pcc.virtual_thread as vt
class Gate:
    @vt.continuation_factory
    def choose(self, value):
        return vt.completed(value)
def parent():
    gate = Gate()
    return gate.choose(42)
task = vt.spawn(parent)
vt.run(1, 32)
print(vt.result(task))
''')
    output = tmp_path / "factory_shape.ll"
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    ir = output.read_text()
    factory = re.search(r"define[^\n]*Gate_choose\([^\n]*\{\n(.*?)\n\}", ir, re.S)
    assert factory and "@py_gen_completed" in factory.group(1)
    assert "Gate_choose__gen_resume" not in ir
    assert "strict.nolib.stub" not in ir


@pytest.mark.parametrize("body", [
    "return vt.completed(42)",
    "vt.yield_now()\n        return vt.completed(42)",
    "slow()\n        return vt.completed(42)",
])
def test_factory_boundaries_fail_closed(tmp_path, body):
    from pcc.py_frontend.pipeline import compile_python

    if body == "return vt.completed(42)":
        source_text = "import pcc.virtual_thread as vt\ndef invalid():\n    return vt.completed(42)\ninvalid()\n"
    else:
        source_text = ("import pcc.virtual_thread as vt\ndef slow():\n    vt.yield_now()\n"
            "class Gate:\n    @vt.continuation_factory\n    def invalid(self):\n        " + body + "\n"
            "def parent():\n    gate = Gate()\n    gate.invalid()\ntask = vt.spawn(parent)\nvt.run(1, 32)\n")
    source = tmp_path / "invalid_factory.py"
    source.write_text(source_text)
    with pytest.raises(Exception, match="factory|resumable|generator"):
        compile_python(str(source), str(tmp_path / "invalid.ll"), emit_llvm_only=True,
                       backend="self", libpython_mode="off", ir_scaffold_mode="on")


def test_factory_fast_slow_and_error_paths_across_collectors(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "factories.py"
    source.write_text('''import gc
import pcc.virtual_thread as vt

def slow(value):
    vt.yield_now()
    gc.collect()
    return value

def failing():
    vt.yield_now()
    raise ValueError("factory failure")

class Gate:
    @vt.continuation_factory
    def choose(self, wait, value):
        if wait:
            return vt.continuation(slow, value)
        return vt.completed(value)

    @vt.continuation_factory
    def fail(self):
        return vt.continuation(failing)

def parent():
    gate = Gate()
    value = [41]
    fast = gate.choose(False, value)
    slow_value = gate.choose(True, value)
    method_fast = gate.choose(False, value)
    method_slow = gate.choose(True, value)
    if fast is not value or slow_value is not value:
        raise RuntimeError("factory lost result identity")
    if method_fast is not value or method_slow is not value:
        raise RuntimeError("method factory lost result identity")
    value.append(42)
    try:
        gate.fail()
    except ValueError as error:
        if str(error) != "factory failure":
            raise RuntimeError("factory lost child exception")
    else:
        raise RuntimeError("factory swallowed child exception")
    return value

task = vt.spawn(parent)
vt.run(1, 256)
print(vt.result(task))
''')
    executable = tmp_path / "factories"
    compile_python(str(source), str(executable), backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(executable)], capture_output=True, text=True,
            timeout=15, env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "[41, 42]"
