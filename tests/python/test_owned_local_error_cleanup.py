"""Owned local slots clear before error-exit root frames are left."""
from __future__ import annotations
import os
from pathlib import Path
import re
import subprocess
import pytest

_ROOT = Path(__file__).resolve().parents[2]
SOURCE = """from pcc.extern import extern, c_int64
import sys
import gc
import weakref
_backend = extern('pcc_gc_backend', (), c_int64)
_threads = extern('pcc_threads_enabled', (), c_int64)
assert _backend() == int(sys.argv[1])
assert _threads() == 1
references = []
events = []

class Tracked:
    def __init__(self, label):
        self.label = label
        references.append(weakref.ref(self))
    def __del__(self):
        events.append(self.label)
        gc.collect()

def fail_two():
    first = Tracked("first")
    second = Tracked("second")
    raise ValueError("two-original")

def fail_branch(make):
    # int(str) creates an error exit before the late object assignment.
    guard = int("3")
    if make:
        late = Tracked("late")
    raise ValueError("branch-original")

def fail_alias():
    first = Tracked("aliased")
    alias = first
    first = Tracked("replacement")
    raise ValueError("alias-original")

def fail_borrowed(borrowed):
    local = Tracked("borrowed-local")
    assert borrowed.label == "caller"
    raise ValueError("borrowed-original")

def fail_for(values):
    for target in values:
        raise ValueError("for-original")

def finish_for(values, mode):
    for target in values:
        if mode == 1:
            break
        if mode == 2:
            return None
    return None

def catch_inside_for(values):
    try:
        for target in values:
            raise ValueError("caught-loop")
    except ValueError as error:
        assert str(error) == "caught-loop"
        assert target is values[0]
        gc.collect()
        assert target.label == "caught-target"
    return None

def fail_element(value):
    raise ValueError("comprehension-original")

def fail_comprehension(values):
    result = [fail_element(value) for value in values]
    return result

def check_dead():
    gc.collect()
    for reference in references:
        assert reference() is None

def main():
    try:
        fail_two()
    except ValueError as error:
        assert str(error) == "two-original"
    else:
        raise AssertionError("missing two error")
    check_dead()
    assert events.count("first") == 1
    assert events.count("second") == 1
    print("two-owned-cleared")
    try:
        fail_branch(False)
    except ValueError as error:
        assert str(error) == "branch-original"
    try:
        fail_branch(True)
    except ValueError as error:
        assert str(error) == "branch-original"
    check_dead()
    assert events.count("late") == 1
    print("late-and-unbound-cleared")
    try:
        fail_alias()
    except ValueError as error:
        assert str(error) == "alias-original"
    check_dead()
    assert events.count("aliased") == 1
    assert events.count("replacement") == 1
    print("alias-and-rebind-cleared")
    caller = Tracked("caller")
    reference = weakref.ref(caller)
    try:
        fail_borrowed(caller)
    except ValueError as error:
        assert str(error) == "borrowed-original"
    assert reference() is caller
    assert events.count("borrowed-local") == 1
    caller = None
    check_dead()
    assert events.count("caller") == 1
    print("borrowed-parameter-preserved")
    values = [Tracked("for-target")]
    try:
        fail_for(values)
    except ValueError as error:
        assert str(error) == "for-original"
    values = None
    check_dead()
    assert events.count("for-target") == 1
    for mode in (0, 1, 2):
        values = [Tracked("normal-for")]
        finish_for(values, mode)
        values = None
        check_dead()
    assert events.count("normal-for") == 3
    values = [Tracked("caught-target")]
    catch_inside_for(values)
    values = None
    check_dead()
    assert events.count("caught-target") == 1
    values = [Tracked("comprehension")]
    try:
        fail_comprehension(values)
    except ValueError as error:
        assert str(error) == "comprehension-original"
    values = None
    check_dead()
    assert events.count("comprehension") == 1
    print("owned-error-cleanup-ok")
main()
"""
EXPECTED = "two-owned-cleared\nlate-and-unbound-cleared\nalias-and-rebind-cleared\nborrowed-parameter-preserved\nowned-error-cleanup-ok\n"

def _configure(monkeypatch):
    for key, value in {"PCC_PYTHON_IR_PASSES":"off", "PCC_PY_FRONTEND_JOBS":"1",
                       "PCC_SELF_BACKEND_JOBS":"1", "PCC_WITH_THREADS":"1",
                       "PCC_REFCOUNT_KIND":"atomic", "PCC_NO_AUTO_PCC1":"1",
                       "PCC_SELF_LINK":"pcc", "PCC_SELF_BACKEND_OBJECT_CACHE":"0",
                       "PCC_PY_FRONTEND_IR_CACHE":"0"}.items():
        monkeypatch.setenv(key, value)

def _source(tmp_path):
    path=tmp_path/"owned_errors.py"
    path.write_text(SOURCE)
    return path

@pytest.mark.parametrize("scaffold", ("on","off"))
def test_owned_error_real_ir_releases_before_all_root_leaves(tmp_path, monkeypatch, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    output=tmp_path/"owned-errors.ll"
    compile_python_multi([str(_source(tmp_path))],str(output),module_names=["owned_errors"],
                         entry_module="owned_errors",backend="self",libpython_mode="off",
                         ir_scaffold_mode=scaffold,emit_llvm_only=True)
    text=output.read_text()
    for name in ("fail_two","fail_branch","fail_alias","fail_borrowed","fail_for"):
        function=re.search(r"^define[^\n]*@user_owned_errors_"+name+r"\([^\n]*\)[^\n]*\{\n(.*?)^\}",text,re.M|re.S)
        assert function is not None,name
        body=function.group(1)
        entry=re.search(r"^err.exit:\n(.*?)(?=^[\w.]+:|\Z)",body,re.M|re.S)
        finish=re.search(r"^err.finish:\n(.*?)(?=^[\w.]+:|\Z)",body,re.M|re.S)
        assert entry is not None and finish is not None,(name,body)
        assert ".err.active.slot" in entry.group(1),(name,entry.group(1))
        assert "@pcc_gc_store_root" in entry.group(1),(name,entry.group(1))
        assert "@pcc_gc_frame_leave" not in entry.group(1)
        assert "br label %err.finish" in entry.group(1)
        assert "@pcc_gc_frame_leave" in finish.group(1)
        assert "@pcc_gc_store_root" not in finish.group(1)
        assert "strict.nolib.stub" not in body

@pytest.fixture
def explicit_runtime():
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest
    requested=os.environ.get("PCC_RUNTIME_ARCHIVE","")
    assert requested,"explicit matching threaded runtime required; do not build"
    runtime=Path(requested).resolve(strict=True)
    verify_runtime_archive_manifest(runtime,runtime_root=_ROOT/"pcc/runtime")
    return runtime

@pytest.mark.integration
@pytest.mark.parametrize("scaffold", ("on","off"))
def test_host_emitted_owned_error_lifetimes(tmp_path,monkeypatch,explicit_runtime,scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    output=tmp_path/"owned-errors"
    compile_python_multi([str(_source(tmp_path))],str(output),module_names=["owned_errors"],
                         entry_module="owned_errors",backend="self",libpython_mode="off",
                         ir_scaffold_mode=scaffold,runtime_archive=str(explicit_runtime))
    for backend in (1,4):
        result=subprocess.run([str(output),str(backend)],cwd=tmp_path,text=True,capture_output=True,
                              timeout=30,env=dict(os.environ,PCC_GC_BACKEND=str(backend),PATH="/nonexistent"))
        (tmp_path/('execute-gc'+str(backend)+'.stdout')).write_text(result.stdout)
        (tmp_path/('execute-gc'+str(backend)+'.stderr')).write_text(result.stderr)
        assert result.returncode==0 and result.stdout==EXPECTED,(backend,result.stdout,result.stderr)


GENERATOR_SOURCE = """from pcc.extern import extern, c_int64
import sys
import gc
import weakref
_backend = extern('pcc_gc_backend', (), c_int64)
_threads = extern('pcc_threads_enabled', (), c_int64)
assert _backend() == int(sys.argv[1])
assert _threads() == 1
references = []
events = []
class Tracked:
    def __del__(self):
        events.append("generator-released")
        gc.collect()
def source_generator():
    value = Tracked()
    references.append(weakref.ref(value))
    yield 1
    raise ValueError("generator-original")
def main():
    generator = source_generator()
    assert next(generator) == 1
    reference = references[0]
    assert reference() is not None
    try:
        next(generator)
    except ValueError as error:
        assert str(error) == "generator-original"
    else:
        raise AssertionError("missing generator exception")
    gc.collect()
    assert reference() is None
    assert events == ["generator-released"]
    # Retain the same generator across the check and prove completion.
    try:
        next(generator)
    except StopIteration:
        print("source-generator-error-cleanup-ok")
    else:
        raise AssertionError("generator resumed after terminal error")
main()
"""

@pytest.mark.integration
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_source_generator_error_releases_frame_while_generator_lives(tmp_path, monkeypatch, explicit_runtime, scaffold):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    source = tmp_path / "source_generator_error.py"
    source.write_text(GENERATOR_SOURCE)
    binary = tmp_path / "source-generator-error"
    compile_python_multi([str(source)], str(binary), module_names=["source_generator_error"],
                         entry_module="source_generator_error", backend="self", libpython_mode="off",
                         ir_scaffold_mode=scaffold, runtime_archive=str(explicit_runtime))
    for backend in (1, 4):
        result = subprocess.run([str(binary), str(backend)], cwd=tmp_path, text=True, capture_output=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"))
        (tmp_path / ("execute-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("execute-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == "source-generator-error-cleanup-ok\n", (backend, result.stdout, result.stderr)


GENERATOR_PREFIX = """from pcc.extern import extern, c_int64
import sys
import gc
import weakref
_backend = extern('pcc_gc_backend', (), c_int64)
_threads = extern('pcc_threads_enabled', (), c_int64)
assert _backend() == int(sys.argv[1])
assert _threads() == 1
references = []
events = []
class Tracked:
    def __del__(self):
        events.append("generator-released")
        gc.collect()
"""
GENERATOR_TERMINAL_CASES = {
    "normal": """def source_generator():
    value = Tracked()
    references.append(weakref.ref(value))
    yield 1

def main():
    generator = source_generator()
    assert next(generator) == 1
    reference = references[0]
    assert reference() is not None
    try:
        next(generator)
    except StopIteration as error:
        assert error.value is None
    else:
        raise AssertionError("missing normal StopIteration")
    gc.collect()
    assert reference() is None
    assert events == ["generator-released"]
    try:
        next(generator)
    except StopIteration:
        print("source-generator-normal-ok")
main()
""",
    "return_alias": """def source_generator():
    value = Tracked()
    references.append(weakref.ref(value))
    yield 1
    return value

def main():
    generator = source_generator()
    assert next(generator) == 1
    reference = references[0]
    try:
        next(generator)
    except StopIteration as error:
        result = error.value
        assert result is reference()
        gc.collect()
        assert result is reference()
        assert events == []
    else:
        raise AssertionError("missing return StopIteration")
    assert result is reference()
    result = None
    gc.collect()
    assert reference() is None
    assert events == ["generator-released"]
    try:
        next(generator)
    except StopIteration:
        print("source-generator-return_alias-ok")
main()
""",
    "caught": """def source_generator():
    value = Tracked()
    references.append(weakref.ref(value))
    yield 1
    try:
        raise ValueError("caught-inside")
    except ValueError as error:
        assert str(error) == "caught-inside"
        gc.collect()
        assert references[0]() is value
        yield 2
    assert references[0]() is value

def main():
    generator = source_generator()
    assert next(generator) == 1
    reference = references[0]
    assert next(generator) == 2
    gc.collect()
    assert reference() is not None
    assert events == []
    try:
        next(generator)
    except StopIteration:
        pass
    else:
        raise AssertionError("missing caught-generator finish")
    gc.collect()
    assert reference() is None
    assert events == ["generator-released"]
    try:
        next(generator)
    except StopIteration:
        print("source-generator-caught-ok")
main()
""",
}

@pytest.mark.integration
@pytest.mark.parametrize("case", ("normal", "return_alias", "caught"))
@pytest.mark.parametrize("scaffold", ("on", "off"))
def test_source_generator_terminal_value_owners(tmp_path, monkeypatch, explicit_runtime, scaffold, case):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    source = tmp_path / "source_generator_terminal.py"
    source.write_text(GENERATOR_PREFIX + GENERATOR_TERMINAL_CASES[case])
    binary = tmp_path / "source-generator-terminal"
    compile_python_multi([str(source)], str(binary), module_names=["source_generator_terminal"],
                         entry_module="source_generator_terminal", backend="self", libpython_mode="off",
                         ir_scaffold_mode=scaffold, runtime_archive=str(explicit_runtime))
    for backend in (1, 4):
        result = subprocess.run([str(binary), str(backend)], cwd=tmp_path, text=True, capture_output=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"))
        (tmp_path / ("execute-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("execute-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == "source-generator-" + case + "-ok\n", (backend, result.stdout, result.stderr)


def test_source_generator_terminal_real_ir_keeps_frame_rooted(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    source = tmp_path / "source_generator_terminal.py"
    source.write_text(GENERATOR_PREFIX + GENERATOR_TERMINAL_CASES["return_alias"])
    output = tmp_path / "source-generator-terminal.ll"
    compile_python_multi([str(source)], str(output), module_names=["source_generator_terminal"],
                         entry_module="source_generator_terminal", backend="self", libpython_mode="off",
                         ir_scaffold_mode="on", emit_llvm_only=True)
    text = output.read_text()
    body = re.search(r"^define[^\n]*@user_source_generator_terminal_source_generator__gen_resume\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert body is not None
    body = body.group(1)
    assert "gen.frame.borrowed.slot" in body
    assert "gen.terminal.frame.keeper" in body
    assert "@pcc_gc_take_pinned_slot" in body
    error = re.search(r"^err.exit:\n(.*?)(?=^[A-Za-z0-9_.]+:|\Z)", body, re.M | re.S)
    assert error is not None
    error = error.group(1)
    assert error.index("@py_gen_set_done") < error.index("@py_gen_frame_set")
    assert "@pcc_gc_frame_leave" not in error
    normal_finish = body.index("@py_gen_finish")
    terminal_clear = body.index("@py_gen_frame_set", normal_finish)
    assert normal_finish < terminal_clear


@pytest.mark.integration
@pytest.mark.parametrize("termination", ("normal", "error"))
def test_terminal_frame_clears_heap_only_owners_during_finalizer_gc(tmp_path, monkeypatch, explicit_runtime, termination):
    from pcc.frontends.python.pipeline import compile_python_multi
    _configure(monkeypatch)
    terminal = "    return payload\n" if termination == "normal" else "    raise ValueError('heap-only-original')\n"
    handler = ("    except StopIteration as error:\n        assert error.value is payload\n"
               if termination == "normal" else
               "    except ValueError as error:\n        assert str(error) == 'heap-only-original'\n")
    body = """def source_generator(payload):
    first = Tracked()
    second = Tracked()
    references.append(weakref.ref(first))
    references.append(weakref.ref(second))
    yield 1
    first = None
    second = None
""" + terminal + """
def main():
    payload = ["exact-return-identity"]
    generator = source_generator(payload)
    assert next(generator) == 1
    first_ref = references[0]
    second_ref = references[1]
    assert first_ref() is not None
    assert second_ref() is not None
    assert first_ref() is not second_ref()
    try:
        next(generator)
""" + handler + """    else:
        raise AssertionError("missing terminal exception")
    gc.collect()
    assert first_ref() is None
    assert second_ref() is None
    assert events == ["generator-released", "generator-released"]
    # The same generator remains alive while both saved-cell owners vanish.
    try:
        next(generator)
    except StopIteration:
        print("source-generator-heap-only-" + """ + repr(termination) + """ + "-ok")
    else:
        raise AssertionError("terminal generator resumed")
main()
"""
    source = tmp_path / "heap_only_generator.py"
    source.write_text(GENERATOR_PREFIX + body)
    binary = tmp_path / "heap-only-generator"
    compile_python_multi([str(source)], str(binary), module_names=["heap_only_generator"],
                         entry_module="heap_only_generator", backend="self", libpython_mode="off",
                         ir_scaffold_mode="on", runtime_archive=str(explicit_runtime))
    for backend in (1, 4):
        result = subprocess.run([str(binary), str(backend)], cwd=tmp_path, text=True, capture_output=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent"))
        (tmp_path / ("execute-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("execute-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == "source-generator-heap-only-" + termination + "-ok\n", (backend, result.stdout, result.stderr)
