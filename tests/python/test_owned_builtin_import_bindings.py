"""Builtin member imports bind actual owned values in the executing scope."""

import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from tests.python.test_hoist_lexical_scope_boundaries import SHADOW_IMPORT


ALIASES = '''from builtins import abs as module_abs, int as Int, list as List
def construct():
    from builtins import abs as calculate, chr as character, repr as represent
    return calculate(-10**40), character(65), represent(42)
def main():
    assert module_abs(-99) == 99
    assert Int is int and List is list
    value, character, representation = construct()
    assert value == 10**40 and character == "A" and representation == "42"
    print("OWNED_BUILTIN_IMPORT_ALIASES_OK")
main()
'''

PROTOCOL = '''class Value:
    def __abs__(self):
        return self
def probe(calculate, value):
    return calculate(value)
def main():
    from builtins import abs as calculate
    value = Value()
    assert probe(calculate, value) is value
    for operands in ((), (1, 2)):
        try:
            calculate(*operands)
        except TypeError:
            pass
        else:
            raise AssertionError("abs accepted invalid arity")
    try:
        calculate(value=1)
    except TypeError:
        pass
    else:
        raise AssertionError("abs accepted a keyword")
    print("OWNED_BUILTIN_IMPORT_PROTOCOL_OK")
main()
'''

LAZY = '''def replacement(value):
    return value + 100
def probe(enabled):
    if enabled:
        from builtins import abs as saved
    try:
        initial = saved(-42)
    except UnboundLocalError:
        assert not enabled
        return "unbound"
    assert enabled and initial == 42
    saved = replacement
    assert saved(2) == 102
    del saved
    try:
        saved(1)
    except UnboundLocalError:
        return "deleted"
    raise AssertionError("deleted local import remained bound")
def main():
    assert probe(False) == "unbound"
    assert probe(True) == "deleted"
    print("OWNED_BUILTIN_IMPORT_LAZY_OK")
main()
'''

PARTIAL = '''def main():
    try:
        from builtins import abs as ready, __pcc_missing_builtin__ as missing
    except ImportError:
        pass
    else:
        raise AssertionError("unknown builtin was accepted")
    assert ready(-42) == 42
    try:
        missing
    except UnboundLocalError:
        pass
    else:
        raise AssertionError("failed import bound its missing member")
    print("OWNED_BUILTIN_IMPORT_PARTIAL_OK")
main()
'''

IDENTITY = '''def first():
    from builtins import abs as calculate
    return calculate
def second():
    from builtins import abs as calculate
    return calculate
def main():
    assert first() is second()
    assert first() is abs
    print("OWNED_BUILTIN_IMPORT_IDENTITY_OK")
main()
'''

GLOBAL_CONDITIONAL = '''def select():
    return delayed(-42)
if False:
    from builtins import abs as delayed
def install():
    global delayed
    from builtins import abs as delayed
def main():
    global delayed
    try:
        select()
    except NameError:
        pass
    else:
        raise AssertionError("unexecuted import created a global")
    install()
    assert select() == 42 and delayed is abs
    del delayed
    try:
        select()
    except NameError:
        pass
    else:
        raise AssertionError("deleted import remained bound")
    print("OWNED_BUILTIN_GLOBAL_CONDITIONAL_OK")
main()
'''

GLOBAL_PARTIAL = '''try:
    from builtins import abs as ready, __pcc_missing_builtin__ as absent
except ImportError:
    pass
else:
    raise AssertionError("unknown builtin was accepted")
def main():
    assert ready(-42) == 42
    try:
        absent
    except NameError:
        pass
    else:
        raise AssertionError("failed import created a global")
    print("OWNED_BUILTIN_GLOBAL_PARTIAL_OK")
main()
'''

THREADS = '''from threading import Thread
def worker(index, results):
    from builtins import abs as calculate
    results[index] = calculate
    assert calculate(-42) == 42
def main():
    results = [None] * 8
    threads = []
    for index in range(8):
        thread = Thread(target=worker, args=(index, results))
        threads.append(thread)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    first = results[0]
    for value in results:
        assert value is first
    assert first is abs
    print("OWNED_BUILTIN_IMPORT_THREADS_OK")
main()
'''

PROGRAMS = {
    "scope_import": (SHADOW_IMPORT, "HOIST_IMPORT_SHADOW_OK\n"),
    "aliases": (ALIASES, "OWNED_BUILTIN_IMPORT_ALIASES_OK\n"),
    "protocol": (PROTOCOL, "OWNED_BUILTIN_IMPORT_PROTOCOL_OK\n"),
    "lazy": (LAZY, "OWNED_BUILTIN_IMPORT_LAZY_OK\n"),
    "partial": (PARTIAL, "OWNED_BUILTIN_IMPORT_PARTIAL_OK\n"),
    "identity": (IDENTITY, "OWNED_BUILTIN_IMPORT_IDENTITY_OK\n"),
    "global_conditional": (GLOBAL_CONDITIONAL, "OWNED_BUILTIN_GLOBAL_CONDITIONAL_OK\n"),
    "global_partial": (GLOBAL_PARTIAL, "OWNED_BUILTIN_GLOBAL_PARTIAL_OK\n"),
}

CROSS_MODULE = {
    "first_provider": '''def select():
    from builtins import abs as calculate
    return calculate
''',
    "second_provider": '''def select():
    from builtins import abs as calculate
    return calculate
''',
    "app": '''from first_provider import select as first
from second_provider import select as second
def main():
    assert first() is second() and first() is abs
    print("OWNED_BUILTIN_IMPORT_CROSS_MODULE_OK")
main()
''',
}


def _generate(source):
    module = type_infer.infer_module(parse_and_lift(source, "<builtin-import>", "builtin_import"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    has_unavailable_function = "strict.nolib.stub:" in text
    assert not has_unavailable_function, "builtin control emitted an unavailable function"
    return text


def test_original_function_scope_builtin_abs_import_uses_owned_protocol():
    text = _generate(SHADOW_IMPORT)
    assert "No module named 'builtins'" not in text
    # The import binds the module's Python wrapper of abs, published as the
    # process-wide builtin function object.
    assert re.search(r"^define\b[^\n]*@user_builtin_import___pcc_builtin_value_abs\(", text, re.M), "missing abs wrapper"
    assert re.search(r"\bcall\b[^\n]*@py_builtin_function_publish\(", text), "missing builtin publication"
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


@pytest.mark.parametrize("name", PROGRAMS)
def test_builtin_import_controls_follow_cpython(capsys, name):
    source, expected = PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


def test_builtin_cold_concurrent_control_follows_cpython(capsys):
    exec(THREADS, {})
    assert capsys.readouterr().out == "OWNED_BUILTIN_IMPORT_THREADS_OK\n"


def test_cross_module_import_values_share_the_runtime_cache(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python_multi
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    names = list(CROSS_MODULE)
    paths = []
    for name, source in CROSS_MODULE.items():
        path = tmp_path / (name + ".py")
        path.write_text(source)
        paths.append(str(path))
    output = tmp_path / "cross-module.ll"
    compile_python_multi(paths, str(output), module_names=names, entry_module="app",
                         emit_llvm_only=True, backend="self", libpython_mode="off",
                         ir_scaffold_mode="on")
    text = output.read_text()
    # Each module publishes its wrapper candidate; the runtime keeps the first.
    assert len(re.findall(r"\bcall\b[^\n]*@py_builtin_function_publish\(", text)) >= 3
    for module_text in re.split(r"^; ---- module: [^\n]* ----\n", text, flags=re.M)[1:]:
        assert len(emit_owned_object(module_text, "arm64-apple-darwin")) > 0


@pytest.mark.parametrize("name", PROGRAMS)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_builtin_import_shapes_reach_owned_objects(name, target):
    text = _generate(PROGRAMS[name][0])
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_builtin_import_controls_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name):
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


@pytest.mark.integration
def test_cold_builtin_import_identity_executes_with_real_threads(tmp_path, monkeypatch, threaded_pcc_runtime_archive, python_program_compiler):
    path = tmp_path / "builtin_threads.py"
    path.write_text(THREADS)
    binary = tmp_path / "builtin_threads"
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    python_program_compiler(str(path), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(threaded_pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        assert result.returncode == 0 and result.stdout == "OWNED_BUILTIN_IMPORT_THREADS_OK\n" and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)


@pytest.mark.integration
def test_cross_module_builtin_identity_executes_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    names = list(CROSS_MODULE)
    paths = []
    for name, source in CROSS_MODULE.items():
        path = tmp_path / (name + ".py")
        path.write_text(source)
        paths.append(str(path))
    binary = tmp_path / "cross_module"
    compile_python_multi(paths, str(binary), module_names=names, entry_module="app",
                         backend="self", libpython_mode="off", ir_scaffold_mode="on",
                         runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        assert result.returncode == 0 and result.stdout == "OWNED_BUILTIN_IMPORT_CROSS_MODULE_OK\n" and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)
