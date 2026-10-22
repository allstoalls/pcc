"""Preprocessed compiler types keep the selected target's actual ABI."""

import builtins
import subprocess

import pytest

from pcc.frontends.c.c_builtin_compat import normalize_builtin_type_compat
from pcc.frontends.c.evaluator.c_evaluator import (
    CEvaluator,
    TranslationUnit,
    _preprocess_translation_unit_source,
)
from pcc.frontends.c.parse import make_c_parser


TARGETS = (
    ("arm64-apple-darwin", ["unsigned", "long"], 8),
    ("x86_64-unknown-linux-gnu", ["unsigned", "long"], 24),
    ("aarch64-unknown-linux-gnu", ["unsigned", "long"], 32),
    ("x86_64-pc-windows-msvc", ["unsigned", "long", "long"], 8),
)


@pytest.fixture(autouse=True)
def deny_external_owners(monkeypatch):
    original = builtins.__import__

    def checked(name, *args, **kwargs):
        if name.split(".")[0] in ("llvmlite", "pycparser", "ply"):
            raise AssertionError("external compiler import: " + name)
        return original(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("external compiler process: " + repr(args))

    monkeypatch.setattr(builtins, "__import__", checked)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.mark.parametrize("target,names,va_size", TARGETS)
@pytest.mark.parametrize("spelling", ("typeof", "__typeof", "__typeof__"))
@pytest.mark.parametrize("alias", ("size_t", "object_extent"))
def test_owned_typeof_sizeof_int_preserves_target_type(target, names, va_size, spelling, alias):
    source = "typedef " + spelling + " ( sizeof ( int ) ) " + alias + ";\n"
    processed = _preprocess_translation_unit_source(source, ".", False, target_triple=target)
    tree = make_c_parser().parse(processed)
    assert tree.ext[0].name == alias
    assert tree.ext[0].type.type.names == names


def _compile(source, target):
    evaluator = CEvaluator(target_triple=target)
    return evaluator.compile_translation_units(
        [TranslationUnit(name="builtin.c", path="", source=source)],
        use_compile_cache=False,
    )[0][1]


@pytest.mark.parametrize("target,names,va_size", TARGETS)
def test_preprocessed_va_list_alias_chain_keeps_target_layout(target, names, va_size):
    source = """
typedef __builtin_va_list __darwin_va_list;
typedef __darwin_va_list va_list;
int builtin_size(void) { return sizeof(__builtin_va_list); }
int alias_size(void) { return sizeof(va_list); }
"""
    processed = _preprocess_translation_unit_source(source, ".", False, target_triple=target)
    assert "typedef __builtin_va_list __darwin_va_list;" in processed
    assert "typedef __darwin_va_list va_list;" in processed
    ir = _compile(source, target)
    assert ir.count("trunc i64 " + str(va_size) + " to i32") == 2


@pytest.mark.parametrize("target,names,va_size", TARGETS)
def test_owned_stdarg_and_preprocessed_input_share_va_list_declaration(target, names, va_size):
    source = "#include <stdarg.h>\nint va_size(void) { return sizeof(va_list); }\n"
    processed = _preprocess_translation_unit_source(source, ".", False, target_triple=target)
    assert normalize_builtin_type_compat(processed, target) == processed
    assert "trunc i64 " + str(va_size) + " to i32" in _compile(source, target)


def test_explicit_builtin_typedef_is_preserved():
    source = "typedef struct { int data[6]; } __builtin_va_list[1];\ntypedef __builtin_va_list va_list;\n"
    assert normalize_builtin_type_compat(source, "x86_64-unknown-linux-gnu") == source


def test_builtin_compat_does_not_rewrite_literals_or_guess_other_typeof():
    source = 'const char *text = "__typeof(sizeof(int)) __builtin_va_list";\ntypedef typeof(missing) invalid;\n'
    assert normalize_builtin_type_compat(source, "arm64-apple-darwin") == source


@pytest.mark.parametrize(
    ('target', 'wchar'),
    [
        ('arm64-apple-darwin', 'int'),
        ('x86_64-unknown-linux-gnu', 'int'),
        ('aarch64-unknown-linux-gnu', 'unsigned int'),
        ('x86_64-pc-windows-msvc', 'unsigned short'),
    ],
)
def test_headerless_keyword_compat_uses_target_wchar_and_real_bool(target, wchar):
    source = 'wchar_t *text; bool enabled(void) { return true; }\n'
    normalized = normalize_builtin_type_compat(source, target)
    assert normalized.startswith('typedef ' + wchar + ' wchar_t;\ntypedef _Bool bool;\n')
    assert 'enum { true = 1 };' in normalized
    tree = make_c_parser().parse(normalized)
    assert tree.ext[1].type.type.names == ['_Bool']


def test_keyword_compat_preserves_explicit_types_values_and_literals():
    source = ('typedef unsigned short wchar_t; typedef unsigned char bool;\n'
              'enum { false = 7, true = 9 };\n'
              'const char *literal = "bool true false wchar_t";\n')
    assert normalize_builtin_type_compat(source, 'aarch64-unknown-linux-gnu') == source
    ordinary = 'int bool; int wchar_t; int true; int false;\n'
    assert normalize_builtin_type_compat(ordinary) == ordinary
    literal = 'const char *text = "bool true false wchar_t";\n'
    assert normalize_builtin_type_compat(literal) == literal


def test_keyword_compat_preserves_comma_and_pointer_declarations():
    source = 'int first, true, false; int *bool; int *wchar_t;\n'
    assert normalize_builtin_type_compat(source) == source


@pytest.mark.parametrize('target,names,va_size', TARGETS)
@pytest.mark.parametrize('parameters', [
    'wchar_t *argv[]',
    'int argc, wchar_t *argv[]',
    'int argc, const wchar_t *argv[]',
    'int argc, wchar_t argv[4]',
])
def test_headerless_wchar_parameter_is_a_type_after_parameter_separator(
    target, names, va_size, parameters,
):
    source = ('int wmain(' + parameters + ') { return 0; }\n'
              'bool dllmain(void) { return true; }\n')
    processed = _preprocess_translation_unit_source(source, '.', False, target_triple=target)
    assert ' wchar_t;' in processed
    tree = make_c_parser().parse(processed)
    definitions = [node for node in tree.ext if type(node).__name__ == 'FuncDef']
    assert [node.decl.name for node in definitions] == ['wmain', 'dllmain']
