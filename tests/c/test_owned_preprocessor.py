"""Owned C preprocessing must resolve inputs and never execute a tool."""

import pytest

from pcc.preprocessor import preprocess
from pcc.evaluater.c_evaluator import CEvaluator, TranslationUnit


def test_header_search_macro_include_and_repeated_unguarded_include(tmp_path):
    headers = tmp_path / "headers"
    headers.mkdir()
    (headers / "values.h").write_text("int VALUE;\n")
    result = preprocess(
        '#define HEADER <values.h>\n#define VALUE first\n#include HEADER\n'
        '#undef VALUE\n#define VALUE second\n#include HEADER\n',
        base_dir=str(tmp_path), include_dirs=[str(headers)],
    )
    assert "int first;" in result
    assert "int second;" in result


def test_nested_quoted_include_guard_and_pragma_once(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.h").write_text('#ifndef A_H\n#define A_H\n#include "b.h"\n#endif\n')
    (sub / "b.h").write_text('#pragma once\n#include "a.h"\nint value;\n')
    result = preprocess('#include "sub/a.h"\n#include "sub/b.h"\n', base_dir=str(tmp_path))
    assert result.count("int value;") == 1


@pytest.mark.parametrize("directive", ['#include "absent.h"', '#include <absent.h>', '#error intentional'])
def test_missing_headers_and_error_are_diagnostics(tmp_path, directive):
    with pytest.raises(RuntimeError, match="absent.h|intentional"):
        preprocess(directive, base_dir=str(tmp_path))


def test_cpp_arguments_apply_in_order_and_load_owned_standard_headers(tmp_path):
    result = preprocess(
        '#include <stdint.h>\n#include <limits.h>\nint32_t x = INT_MAX;\nint v = VALUE;\n',
        cpp_args=["-DVALUE=1", "-UVALUE", "-D", "VALUE=42"],
    )
    assert "typedef int int32_t;" in result
    assert "int32_t x = 2147483647;" in result
    assert "int v = 42;" in result
    assert "typedef long size_t;" not in result


def test_macro_expansion_respects_literals_and_comments():
    result = preprocess('#define NAME value\nconst char *NAME = "NAME"; /* NAME */\n')
    assert 'const char *value = "NAME";' in result


def test_self_c_frontend_does_not_probe_or_run_a_system_preprocessor(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external preprocessor was consulted")

    monkeypatch.setattr(CEvaluator, "_has_system_cpp", forbidden)
    monkeypatch.setattr(CEvaluator, "_system_cpp", forbidden)
    ev = CEvaluator(backend="self")
    unit = TranslationUnit(name="probe.c", path="probe.c", source=(
        '#include <stdint.h>\nint add(int32_t a, int32_t b) { return a + b; }\n'
        'int main(void) { return add(20, VALUE); }\n'
    ))
    result = ev.compile_translation_units([unit], cpp_args=["-DVALUE=22"], use_compile_cache=False)
    assert result


def test_self_c_emitted_execution_with_tool_and_llvm_denial(tmp_path):
    import os
    import subprocess
    import sys

    # Install the guards before importing pcc so an eager optional import
    # cannot hide behind modules loaded by another test.
    script = '''
import builtins, subprocess, sys
original_import = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name == "llvmlite" or name.startswith("llvmlite."):
        raise AssertionError("LLVM import attempted: " + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = checked_import
def forbidden(*args, **kwargs):
    raise AssertionError("external tool attempted: " + repr(args))
subprocess.Popen = forbidden
from pcc.evaluater.c_evaluator import CEvaluator, TranslationUnit
ev = CEvaluator(backend="self")
ev._system_cc = forbidden
source = '#include <stdio.h>\\n#include <stdint.h>\\nint add(int32_t a,int32_t b){return a+b;}\\nint main(void){printf("%d\\\\n",add(20,VALUE));return 0;}\\n'
units = ev.compile_translation_units([TranslationUnit(name="probe.c", path="probe.c", source=source)], cpp_args=["-DVALUE=22"], use_compile_cache=False)
prepared = ev._prepare_self_backend_units(units, optimize=1)
ev._link_executable_owned(ev._self_backend_asm_text(prepared), sys.argv[1])
'''
    exe = tmp_path / "owned_c"
    built = subprocess.run([sys.executable, "-c", script, str(exe)], capture_output=True, text=True, timeout=20)
    assert built.returncode == 0, built.stdout + built.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"


def test_c_output_option_publishes_without_running_or_delegating(tmp_path, monkeypatch):
    import subprocess
    from pcc import project
    from pcc.cli_core import cli_main

    source = tmp_path / "program.c"
    source.write_text('#include <stdio.h>\nint add(int a,int b){return a+b;}\nint main(void){printf("%d\\n",add(20,22));return 0;}\n')
    output = tmp_path / "program"
    with monkeypatch.context() as patch:
        patch.setattr(CEvaluator, "_system_cc", lambda: pytest.fail("C output requested cc"))
        patch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("C output launched a subprocess"))
        patch.setattr(project, "run_prepare_commands", lambda *a, **k: pytest.fail("unrequested prepare hook"))
        patch.setattr(project, "ensure_make_goals", lambda *a, **k: pytest.fail("unrequested make hook"))
        assert cli_main(["--backend", "self", "--no-cache", str(source), "-o", str(output)]) == 0
    run = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"
