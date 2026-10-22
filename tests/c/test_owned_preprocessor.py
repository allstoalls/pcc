"""Owned C preprocessing must resolve inputs and never execute a tool."""

import pytest

from pcc.frontends.c.preprocessor import preprocess
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator, TranslationUnit


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


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("-std=c89", "none"),
        ("-ansi", "none"),
        ("-std=gnu99", "199901L"),
        ("-std=c11", "201112L"),
        ("-std=c17", "201710L"),
    ],
)
def test_language_standard_sets_stdc_version(option, expected):
    # The c-testsuite cases compile with -std=c89; the owned preprocessor
    # rejected every -std option, so all of them failed before parsing.
    result = preprocess(
        "#ifdef __STDC_VERSION__\nlong v = __STDC_VERSION__;\n#else\nint none;\n#endif\n",
        cpp_args=[option],
    )
    if expected == "none":
        assert "int none;" in result
    else:
        assert "long v = " + expected + ";" in result


def test_unknown_language_standard_fails_closed():
    with pytest.raises(ValueError, match="-std=c42"):
        preprocess("int x;\n", cpp_args=["-std=c42"])


def test_macro_expansion_respects_literals_and_comments():
    result = preprocess('#define NAME value\nconst char *NAME = "NAME"; /* NAME */\n')
    assert 'const char *value = "NAME";' in result


def test_function_macro_arguments_can_span_physical_lines():
    result = preprocess(
        '#define DEPRECATED(message)\n'
        'DEPRECATED("first ( fragment"\n "second ) fragment") int value;\n'
        '#define ADD(left, right) ((left) + (right))\n'
        'int sum = ADD(20,\n ADD(10,\n 12));\n'
    )
    assert 'int value;' in result
    assert 'DEPRECATED' not in result
    assert 'ADD' not in result
    assert '((20) + (((10) + (12))))' in result


def test_function_macro_name_and_opening_can_span_physical_lines():
    result = preprocess('#define VALUE(x) x\nint value = VALUE\n\n(42);\n')
    assert 'int value = 42;' in result


def test_line_after_multiline_macro_keeps_physical_line_number():
    result = preprocess('#define DROP(x)\nDROP(\n  ignored\n)\nint line = __LINE__;\n')
    assert 'int line = 5;' in result


def test_truly_unterminated_multiline_macro_is_a_diagnostic():
    with pytest.raises(RuntimeError, match='unterminated macro invocation: VALUE'):
        preprocess('#define VALUE(x) x\nint value = VALUE(\n42;\n')


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


def test_self_c_emitted_execution_with_tool_and_llvm_denial(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    import os
    import subprocess
    import sys

    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

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
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator, TranslationUnit
ev = CEvaluator(backend="self")
ev._system_cc = forbidden
source = '#include <stdio.h>\\n#include <stdint.h>\\nint add(int32_t a,int32_t b){return a+b;}\\nint main(void){printf("%d\\\\n",add(20,VALUE));return 0;}\\n'
units = ev.compile_translation_units([TranslationUnit(name="probe.c", path="probe.c", source=source)], cpp_args=["-DVALUE=22"], use_compile_cache=False)
prepared = ev._prepare_self_backend_units(units, optimize=1)
ev.emit_executable(prepared, sys.argv[1], optimize=False)
'''
    exe = tmp_path / "owned_c"
    built = subprocess.run([sys.executable, "-c", script, str(exe)], capture_output=True, text=True, timeout=20)
    assert built.returncode == 0, built.stdout + built.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"


def test_c_output_option_publishes_without_running_or_delegating(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    import subprocess
    from pcc.driver import project
    from pcc.driver.cli_core import cli_main

    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
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


def test_literal_and_expanded_stringification():
    result = preprocess('#define STRING(x) #x\n#define EXPAND_STRING(x) STRING(x)\n#define VERSION 1.10.0\nchar *raw = STRING(VERSION);\nchar *expanded = EXPAND_STRING(VERSION);\n')
    assert 'char *raw = "VERSION";' in result
    assert 'char *expanded = "1.10.0";' in result

def test_substitution_preserves_literals_and_argument_escaping():
    result = preprocess('#define TEXT(x) "x" x\n#define STRING(x) #x\nchar *text = TEXT("z");\nchar *quoted = STRING("a  b\\n");\n')
    assert 'char *text = "x" "z";' in result
    assert 'char *quoted = "\\"a  b\\\\n\\"";' in result

def test_raw_token_paste_and_argument_prescan():
    result = preprocess('#define CAT(a,b) a##b\n#define XCAT(a,b) CAT(a,b)\n#define NAME foo\nint CAT(NAME,bar);\nint XCAT(NAME,bar);\nint CAT(,tail);\nint CAT(head,);\n')
    assert 'int NAMEbar;' in result
    assert 'int foobar;' in result
    assert 'int tail;' in result
    assert 'int head;' in result

def test_macro_substitution_preserves_adjacent_operators():
    result = preprocess('#define BUMP(x) x++\nint value = BUMP(n);\n')
    assert 'int value = n++;' in result

def test_gnu_optional_variadic_comma():
    result = preprocess('#define CALL(f,...) f(0,##__VA_ARGS__)\nint a=CALL(foo);\nint b=CALL(foo,1,2);\nint c=CALL(foo,);\n')
    assert 'int a=foo(0);' in result
    assert 'int b=foo(0,1, 2);' in result
    assert 'int c=foo(0,);' in result


def test_c23_elifdef_and_elifndef_select_one_branch():
    source = (
        "#define HAVE_A 1\n"
        "#if 0\nint zero;\n#elifdef MISSING\nint missing;\n#elifdef HAVE_A\nint a;\n"
        "#else\nint other;\n#endif\n"
        "#ifdef MISSING\nint m;\n#elifndef ALSO_MISSING\nint not_defined;\n#endif\n"
        "#if 0\n#if 1\n#elifdef HAVE_A\nint nested;\n#endif\n#endif\n"
    )
    result = preprocess(source)
    assert "int a;" in result
    assert "int not_defined;" in result
    for absent in ("int zero;", "int missing;", "int other;", "int m;", "int nested;"):
        assert absent not in result


def test_feature_test_operators_answer_from_owned_capabilities(tmp_path):
    (tmp_path / "present.h").write_text("int present;\n")
    source = (
        '#if __has_include("present.h") && !__has_include(<absent_header.h>)\n'
        "int include_ok;\n#endif\n"
        "#if defined(__has_include) && defined(__has_feature)\nint defined_ok;\n#endif\n"
        "#ifdef __has_builtin\nint ifdef_ok;\n#endif\n"
        "#if __has_feature(c_static_assert) && !__has_feature(unknown_feature)\n"
        "int feature_ok;\n#endif\n"
        "#if !__has_builtin(__builtin_expect) && !__has_attribute(noreturn)\n"
        "int unclaimed_ok;\n#endif\n"
        "#if !__is_identifier(nullptr) && __is_identifier(plain_name)\n"
        "int identifier_ok;\n#endif\n"
    )
    result = preprocess(source, base_dir=str(tmp_path))
    for marker in ("include_ok", "defined_ok", "ifdef_ok", "feature_ok",
                   "unclaimed_ok", "identifier_ok"):
        assert "int " + marker + ";" in result


def test_nullability_feature_follows_qualifier_erasure():
    source = "#if __has_feature(nullability)\nint claimed;\n#else\nint unclaimed;\n#endif\n"
    assert "int unclaimed;" in preprocess(source)
    assert "int claimed;" in preprocess(source, cpp_args=["-D_Nonnull="])


def test_program_fallback_macro_overrides_feature_operator():
    source = "#define __has_feature(x) 1\n#if __has_feature(anything)\nint program_macro;\n#endif\n"
    assert "int program_macro;" in preprocess(source)


def test_block_storage_class_follows_target_and_blocks_options():
    source = "__block int counter;\n"
    darwin, linux = "arm64-apple-darwin", "x86_64-unknown-linux-gnu"
    assert "__block" not in preprocess(source, target_triple=darwin)
    assert "__block int counter;" in preprocess(source, target_triple=linux)
    assert "__block" not in preprocess(source, target_triple=linux, cpp_args=["-fblocks"])
    assert "__block int counter;" in preprocess(
        source, target_triple=darwin, cpp_args=["-fno-blocks"],
    )
