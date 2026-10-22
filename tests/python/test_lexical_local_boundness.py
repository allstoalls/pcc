"""A local's initialized state is independent of its scalar/owned lane."""

import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift


NUMERIC = '''def probe(enabled):
    if enabled:
        integer = 0
        floating = 0.0
        boolean = False
    try:
        value = integer, floating, boolean
    except UnboundLocalError:
        assert not enabled
        return "unbound"
    assert value == (0, 0.0, False)
    del integer
    try:
        integer
    except UnboundLocalError:
        return "deleted"
    raise AssertionError("deleted integer remained bound")
def main():
    assert probe(False) == "unbound"
    assert probe(True) == "deleted"
    print("LOCAL_NUMERIC_BOUNDNESS_OK")
main()
'''

OBJECT = '''def consume(parameter):
    assert parameter is None
    del parameter
    try:
        parameter
    except UnboundLocalError:
        return True
    raise AssertionError("deleted parameter remained bound")
def probe(enabled):
    if enabled:
        value = None
    try:
        value
    except UnboundLocalError:
        assert not enabled
        return "unbound"
    assert value is None
    value = [1]
    try:
        raise ValueError("switch")
    except ValueError:
        assert value == [1]
    finally:
        value = [2]
    del value
    try:
        value
    except UnboundLocalError:
        value = None
    assert value is None
    return "rebound"
def main():
    assert consume(None)
    assert probe(False) == "unbound"
    assert probe(True) == "rebound"
    print("LOCAL_OBJECT_BOUNDNESS_OK")
main()
'''

LOOPS = '''def probe(count):
    for integer in range(count):
        assert integer >= 0
    try:
        integer
    except UnboundLocalError:
        assert count == 0
        return "empty"
    assert integer == count - 1
    for item in [None, 0, False]:
        assert item is None or item == 0
    assert item is False
    for key, value in {"answer": None}.items():
        assert key == "answer" and value is None
    assert value is None
    return "bound"
def main():
    assert probe(0) == "empty"
    assert probe(2) == "bound"
    print("LOCAL_LOOP_BOUNDNESS_OK")
main()
'''

EXCEPTION = '''def probe(mode):
    try:
        try:
            raise ValueError("original")
        except ValueError as error:
            assert isinstance(error, ValueError)
            if mode:
                raise RuntimeError("replacement")
        finally:
            try:
                error
            except UnboundLocalError:
                pass
            else:
                raise AssertionError("handler target survived into finally")
    except RuntimeError:
        assert mode
    try:
        error
    except UnboundLocalError:
        return True
    raise AssertionError("handler target remained bound")
def main():
    assert probe(False)
    assert probe(True)
    print("LOCAL_EXCEPTION_BOUNDNESS_OK")
main()
'''

CLOSURE = '''def outer():
    value = None
    def read():
        return value
    def replace():
        nonlocal value
        value = [42]
    assert read() is None
    replace()
    assert read() == [42]
    return value
def main():
    assert outer() == [42]
    print("LOCAL_CLOSURE_BOUNDNESS_OK")
main()
'''

PRIOR_READ = '''def inner(value):
    return 99
def probe():
    try:
        inner
    except UnboundLocalError:
        pass
    else:
        raise AssertionError("local read used the module binding")
    inner = [42]
    return inner[0]
def main():
    assert probe() == 42
    print("LOCAL_PRIOR_READ_OK")
main()
'''

PROGRAMS = {
    "numeric": (NUMERIC, "LOCAL_NUMERIC_BOUNDNESS_OK\n"),
    "object": (OBJECT, "LOCAL_OBJECT_BOUNDNESS_OK\n"),
    "loops": (LOOPS, "LOCAL_LOOP_BOUNDNESS_OK\n"),
    "exception": (EXCEPTION, "LOCAL_EXCEPTION_BOUNDNESS_OK\n"),
    "closure": (CLOSURE, "LOCAL_CLOSURE_BOUNDNESS_OK\n"),
    "prior_read": (PRIOR_READ, "LOCAL_PRIOR_READ_OK\n"),
}


def _generate(source):
    module = type_infer.infer_module(parse_and_lift(source, "<local-boundness>", "local_boundness"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    return str(codegen.generate(module))


@pytest.mark.parametrize("name", PROGRAMS)
def test_local_boundness_controls_follow_cpython(capsys, name):
    source, expected = PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


def test_numeric_local_uses_a_separate_bound_bit_and_unbound_exception():
    text = _generate(NUMERIC)
    assert ".bound.integer.owned" in text
    assert "integer.bound.error" in text
    assert re.search(r"@py_exc_new\(i64 63,", text)


@pytest.mark.parametrize("name", PROGRAMS)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_boundness_shapes_reach_owned_objects(name, target):
    text = _generate(PROGRAMS[name][0])
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_local_boundness_controls_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name):
    source, expected = PROGRAMS[name]
    path = tmp_path / (name + ".py")
    path.write_text(source)
    binary = tmp_path / name
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(path), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        assert result.returncode == 0 and result.stdout == expected and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)
