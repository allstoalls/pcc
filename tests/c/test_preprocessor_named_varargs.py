"""GNU named varargs share the standard token substitution semantics."""

import subprocess
from pathlib import Path

import pytest

from pcc.frontends.c.preprocessor import (
    _CPP_TOKEN_RE,
    preprocess,
)


VARIADIC_SPELLINGS = (
    ('...', '__VA_ARGS__'),
    ('args...', 'args'),
    ('values ...', 'values'),
)


@pytest.fixture(autouse=True)
def forbid_external_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('preprocessing started an external process')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)


@pytest.mark.parametrize('declaration,name', VARIADIC_SPELLINGS)
def test_variadic_arguments_bind_and_prescan_nested_macros(declaration, name):
    source = ('#define ONE 1\n#define TWO 2\n'
              '#define ADD(a,b) ((a)+(b))\n'
              '#define FORWARD(' + declaration + ') ADD(' + name + ')\n'
              'FORWARD(ONE, TWO)\n')
    assert preprocess(source) == '((1)+(2))'


@pytest.mark.parametrize('declaration,name', VARIADIC_SPELLINGS)
def test_variadic_comma_elision_distinguishes_missing_and_explicit_empty(declaration, name):
    source = ('#define CALL(f,' + declaration + ') f(0,##' + name + ')\n'
              'CALL(target)\nCALL(target,)\nCALL(target,1,2)\n')
    assert preprocess(source).splitlines() == [
        'target(0)', 'target(0,)', 'target(0,1, 2)',
    ]


@pytest.mark.parametrize('declaration,name', VARIADIC_SPELLINGS)
def test_only_variadic_parameter_accepts_empty_and_nonempty_arguments(declaration, name):
    source = ('#define CALL(' + declaration + ') target(0,##' + name + ')\n'
              'CALL()\nCALL(1,2)\n')
    assert preprocess(source).splitlines() == ['target(0)', 'target(0,1, 2)']


@pytest.mark.parametrize('declaration,name', VARIADIC_SPELLINGS)
def test_variadic_stringification_uses_raw_tokens_and_preserves_literals(declaration, name):
    source = ('#define VALUE 7\n#define STR(' + declaration + ') #' + name + '\n'
              'STR(VALUE, "a  b")\nSTR()\n')
    assert preprocess(source).splitlines() == ['"VALUE, \\"a  b\\""', '""']


@pytest.mark.parametrize('declaration,name', VARIADIC_SPELLINGS)
def test_variadic_paste_uses_raw_argument_and_accepts_empty_operands(declaration, name):
    source = ('#define NAME expanded\n'
              '#define CAT(prefix,' + declaration + ') prefix##' + name + '\n'
              'CAT(item_,NAME)\nCAT(item_,)\nCAT(,tail)\n')
    assert preprocess(source).splitlines() == ['item_NAME', 'item_', 'tail']


@pytest.mark.parametrize('declaration,name', VARIADIC_SPELLINGS)
def test_variadic_parameter_does_not_rewrite_other_identifiers_or_literals(declaration, name):
    source = ('#define TEXT(' + declaration + ') "' + name + '" ' + name + '_suffix ' + name + '\n'
              'TEXT(7)\n')
    assert preprocess(source) == '"' + name + '" ' + name + '_suffix 7'


def test_ordinary_parameter_named_args_remains_nonvariadic():
    assert preprocess('#define F(args) "args" args_suffix args\nF(9)\n') == '"args" args_suffix 9'


@pytest.mark.parametrize('filename', ['fprintf-1.c', 'fprintf-chk-1.c'])
def test_original_gcc_fprintf_sources_bind_named_variadic_arguments(filename):
    source = Path(__file__).resolve().parents[2] / 'projects' / 'gcc-torture-execute' / filename
    processed = preprocess(source.read_text(), base_dir=str(source.parent))
    tokens = _CPP_TOKEN_RE.findall(processed)
    assert 'args' not in tokens
    assert '"hello"' in tokens
    assert 'abort' in tokens


def test_nonvariadic_comma_paste_does_not_acquire_variadic_extension():
    with pytest.raises(RuntimeError, match='invalid token paste'):
        preprocess('#define F(x) ,##ordinary\nF(1)\n')
