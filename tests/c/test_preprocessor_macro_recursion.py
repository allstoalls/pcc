"""Macro rescanning preserves unavailable-token state instead of growing text."""

from pathlib import Path
import subprocess

import pytest

from pcc.frontends.c.preprocessor import (
    _CPP_TOKEN_RE,
    preprocess,
)


@pytest.fixture(autouse=True)
def forbid_external_processes(monkeypatch, request):
    if request.node.get_closest_marker("integration") is not None:
        return

    def forbidden(*args, **kwargs):
        raise AssertionError("preprocessing started an external process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def assert_tokens(source, expected):
    assert _CPP_TOKEN_RE.findall(preprocess(source)) == _CPP_TOKEN_RE.findall(expected)


def test_function_macro_can_call_function_of_same_name():
    assert_tokens(
        '#define QUOTE_(s) #s\n#define QUOTE(s) QUOTE_(s)\n'
        '#define check(t) check(QUOTE(t), __alignof__(t))\n'
        'check(void); check(long double);\n',
        'check("void", __alignof__(void)); '
        'check("long double", __alignof__(long double));',
    )


def test_original_alignment_case_has_bounded_preprocessing_output():
    path = Path(__file__).resolve().parents[2] / 'projects' / 'gcc-torture-execute' / '20000223-1.c'
    output = preprocess(path.read_text(), base_dir=str(path.parent))
    assert len(output) < 4096
    assert output.count('check (') + output.count('check(') == 17
    assert 'check("long double", __alignof__(long double))' in output
    assert 'check("void (*)()", __alignof__(void (*)()))' in output


@pytest.mark.parametrize(('definitions', 'source', 'expected'), [
    ('#define VALUE VALUE + 1\n', 'VALUE VALUE', 'VALUE + 1 VALUE + 1'),
    ('#define A B\n#define B A\n', 'A B', 'A B'),
    ('#define A(x) B(x)\n#define B(x) A(x)\n', 'A(1) B(2)', 'A(1) B(2)'),
    ('#define F(x) F(x)\n', 'F(F(1)) F(2)', 'F(F(1)) F(2)'),
])
def test_direct_and_indirect_recursion_stops_per_token(definitions, source, expected):
    assert_tokens(definitions + source, expected)


def test_nested_same_name_arguments_are_prescanned_before_disabling_outer_macro():
    assert_tokens('#define DUP(x) x x\nDUP(DUP(1))\n', '1 1 1 1')


def test_argument_prescan_retains_unavailable_tokens_during_outer_rescan():
    assert_tokens('#define F(x) x\n#define G F\nF(G)(2) F(3)\n', 'F(2) 3')


def test_replacement_alias_rescans_with_following_source_parentheses():
    assert_tokens(
        '#define ALIAS VALUE\n#define VALUE(x) ((x)+1)\n'
        '#define ID(x) x\nALIAS(2) ID(VALUE)(3)\n',
        '((2)+1) ((3)+1)',
    )


def test_function_invocation_intersects_name_and_closing_parenthesis_hide_sets():
    assert_tokens('#define A F\n#define F(x) A\nA(1)\n', 'F')


def test_paste_rescans_new_macro_name_without_reenabling_recursive_name():
    assert_tokens(
        '#define CAT(a,b) a##b\n#define VALUE 7\n'
        '#define F(x) CAT(F,)(x)\nCAT(VAL,UE) F(1)\n',
        '7 F(1)',
    )


def test_paste_preserves_unavailable_tokens_in_argument_prefix_and_suffix():
    assert_tokens(
        '#define A A +\n#define CAT(a,b) a##b\n'
        '#define WRAP(x) CAT(x, + 1)\nWRAP(A)\n',
        'A ++ 1',
    )


def test_stringification_does_not_expand_a_recursive_argument():
    assert_tokens('#define F(x) F(x)\n#define S(x) #x\nS(F(1))\n', '"F(1)"')


def test_empty_replacements_do_not_join_preprocessing_tokens():
    assert_tokens('#define EMPTY(x)\n+EMPTY(1)+ /EMPTY(2)*\n', '+ + / *')


def test_long_acyclic_macro_chain_has_no_fixed_point_pass_limit():
    definitions = ''.join('#define M' + str(i) + ' M' + str(i + 1) + '\n' for i in range(40))
    assert preprocess(definitions + '#define M40 7\nM0\n') == '7'


def test_standard_rescan_example_with_nested_calls_and_aliases():
    assert_tokens(
        '#define x 2\n#define f(a) f(x * (a))\n#define g f\n'
        '#define z z[0]\n#define h g(~\n#define m(a) a(w)\n'
        '#define w 0,1\n#define t(a) a\n'
        'f(y+1) + f(f(z)) % t(t(g)(0) + t)(1);\n'
        'g(x+(3,4)-w) | h 5) & m(f)^m(m);\n',
        'f(2 * (y+1)) + f(2 * (f(2 * (z[0])))) % f(2 * (0)) + t(1);\n'
        'f(2 * (2+(3,4)-0,1)) | f(2 * (~ 5)) & f(2 * (0,1))^m(0,1);',
    )


def test_object_macro_token_paste_also_keeps_its_name_disabled():
    assert_tokens('#define SELF SE##LF\n#define VALUE VAL##UE\nSELF VALUE\n', 'SELF VALUE')


@pytest.mark.parametrize('source', [
    '#define F(x) x\nF(1,2)\n',
    '#define F(x,y) x+y\nF(1)\n',
    '#define F() 1\nF(1)\n',
])
def test_wrong_macro_arity_is_a_diagnostic(source):
    with pytest.raises(RuntimeError, match='wrong number of arguments for macro: F'):
        preprocess(source)


@pytest.mark.integration
def test_owned_execution_preserves_recursive_macro_rescanning():
    from tests.owned_c_execution import compile_and_run_owned_c

    result = compile_and_run_owned_c(
        'int call(int value) { return value; }\n'
        'int left(int value) { return value + 10; }\n'
        'int right(int value) { return value + 20; }\n'
        '#define call(x) call((x)+1)\n'
        '#define left(x) right(x)\n#define right(x) left(x)\n'
        '#define CAT(a,b) a##b\n#define DUP(x) ((x)+(x))\n'
        'int main(void) {\n'
        '  if (call(3) != 4 || call(call(3)) != 5) return 1;\n'
        '  if (left(1) != 11 || right(2) != 22) return 2;\n'
        '  if (CAT(ca,ll)(4) != 5 || DUP(DUP(2)) != 8) return 3;\n'
        '  return 0;\n}\n'
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.integration
def test_owned_execution_original_alignment_macro_case():
    from tests.owned_c_execution import compile_and_run_owned_c

    path = Path(__file__).resolve().parents[2] / 'projects' / 'gcc-torture-execute' / '20000223-1.c'
    result = compile_and_run_owned_c(path.read_text())
    assert result.returncode == 0, result.stderr
