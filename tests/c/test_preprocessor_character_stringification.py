"""Character conditions and stringification execute through the owned C path."""

from pathlib import Path
import platform
import sys

import pytest

from pcc.frontends.c.preprocessor import _CppExprError, _eval_cpp_expr, preprocess
from tests.c.test_owned_atomic_scalar_regressions import _run_owned


STRINGIFY_SPACING = r'''
#define STRING(x) #x
#define EXPAND(x) STRING(x)
#define MAJOR 1
#define MINOR 10
#define PATCH 0
#define VERSION MAJOR.MINOR.PATCH
#define SUM a+b
#define SPACED a + b
#define PLUS +
#define CAT(a,b) a##b
int equal(const char *a, const char *b) {
    int i = 0;
    while (a[i] && b[i]) { if (a[i] != b[i]) return 0; i++; }
    return a[i] == b[i];
}
int main(void) {
    if (!equal(EXPAND(VERSION), "1.10.0")) return 1;
    if (!equal(STRING(VERSION), "VERSION")) return 2;
    if (!equal(EXPAND(SUM), "a+b")) return 3;
    if (!equal(EXPAND(SPACED), "a + b")) return 4;
    if (!equal(STRING(  a   +\
      b  ), "a + b")) return 5;
    if (!equal(STRING(a/**/+/**/b), "a + b")) return 6;
    if (!equal(STRING("a  b\n"), "\"a  b\\n\"")) return 7;
    if (!equal(EXPAND(PLUS+), "++")) return 8;
    if (!equal(EXPAND(CAT(1,0).0), "10.0")) return 9;
    { int x = 2; if (x PLUS+1 != 3 || x != 2) return 10; }
    return 0;
}
'''


CHARACTER_CONDITIONS = r'''
#define LETTER 'A'
#define NEXT(x) ((x) + 1)
#if LETTER != 65 || '0' != 48 || NEXT('a') != 'b'
#error ordinary character constants
#endif
#if '\n' != 10 || '\t' != 9 || '\r' != 13 || '\a' != 7 || '\b' != 8 || '\v' != 11 || '\f' != 12
#error simple escapes
#endif
#if '\'' != 39 || '\"' != 34 || '\\' != 92 || '\?' != 63
#error quoted escapes
#endif
#if '\101' != 'A' || '\x41' != 'A' || '\0' != 0
#error numeric escapes
#endif
#if L'A' != 65 || u'\u00e9' != 233 || U'\U0001f600' != 128512
#error prefixed constants
#endif
#if UNKNOWN_IDENTIFIER != 0 || '\x41' == 0 || 'A' == 0
#error unknown identifier replacement damaged literal
#endif
#if (0 && (1 / 0)) || (1 ? '0' : 1 / 0) != 48
#error short circuit with character constants
#endif
#define FLAG
#define OTHER
#if !defined(FLAG) || !defined OTHER || defined(MISSING)
#error defined operators
#endif
#if '/*' != 0x2f2a || '//' != 0x2f2f
#error comment-like character constants
#endif
int choose(void) {
#if 'A' == 65 && '0' == 48
    return 42;
#else
    return 1;
#endif
}
int main(void) { return choose() != 42; }
'''


@pytest.mark.parametrize("source", [CHARACTER_CONDITIONS, STRINGIFY_SPACING], ids=["characters", "spacing"])
# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
def test_owned_character_and_stringification_semantics(source, tmp_path, monkeypatch):
    result = _run_owned(source, tmp_path, monkeypatch)
    assert result.returncode == 0, result
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("relative,body", [
    ("lua-5.5.0/lctype.h", "return LUA_USE_CTYPE;"),
    ("lz4-1.10.0/lib/lz4.h", 'const char *v = LZ4_VERSION_STRING; const char *expected = "1.10.0"; int i = 0; while (expected[i]) { if (v[i] != expected[i]) return i + 1; i++; } return v[i];'),
], ids=["lua-original-header", "lz4-original-header"])
# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
def test_original_vendor_header_semantics(relative, body, tmp_path, monkeypatch):
    header = Path(__file__).resolve().parents[2] / "projects" / relative
    source = '#include "' + str(header) + '"\nint main(void) { ' + body + ' }\n'
    result = _run_owned(source, tmp_path, monkeypatch)
    assert result.returncode == 0, result


@pytest.mark.parametrize("literal", ["''", "'a", r"'\x'", r"'\u123'", r"'\q'", r"u'\u0041'", r"U'\U00110000'", r"u'\ud800'"])
def test_malformed_character_constants_are_diagnostics(literal):
    with pytest.raises(_CppExprError):
        _eval_cpp_expr(literal)


def test_stringification_keeps_actual_whitespace_and_separate_output_tokens():
    source = '#define S(x) #x\n#define X(x) S(x)\n#define A 1\n#define B 10\n#define C 0\n#define V A.B.C\n#define PLUS +\nX(V) X(A . B . C) PLUS+1\n'
    assert preprocess(source) == '"1.10.0" "1 . 10 . 0" + +1'
