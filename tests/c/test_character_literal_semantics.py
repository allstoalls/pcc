"""C character values and types agree in preprocessing, AST and emitted SSA."""

from pathlib import Path
import platform
import subprocess
import sys

import pytest

from pcc.backend.owned_elf_link import link_inputs
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator
from pcc.frontends.c.parse import make_c_parser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass
from pcc.frontends.c.preprocessor import _CppExprError, _eval_cpp_expr, preprocess
from pcc.frontends.c.ssa.builder import SSABuilder
from tests.c.test_owned_atomic_scalar_regressions import TARGETS, _forbid_process


CHARACTERS = r'''
#ifdef __CHAR_UNSIGNED__
#define HIGH_BYTE 255
#else
#define HIGH_BYTE (-1)
#endif
#if '\xff' != HIGH_BYTE || '\377' != HIGH_BYTE || 'AB' != 0x4142
#error character preprocessing value
#endif
#if 'é' != 0xc3a9 || '\u00e9' != 'é' || U'\U0001f600' != 128512
#error character encoding value
#endif
_Static_assert(_Generic('A', int: 1, default: 0), "ordinary int type");
_Static_assert(_Generic(u'A', unsigned short: 1, default: 0), "UTF-16 type");
_Static_assert(_Generic(U'A', unsigned int: 1, default: 0), "UTF-32 type");
_Static_assert(_Generic(L'A', __WCHAR_TYPE__: 1, default: 0), "wide type");
enum values { latin = u'\u00e9', emoji = U'\U0001f600', pair = 'AB' };
static int initial_byte = '\xff';
static int initial_latin = u'\u00e9';
int character_value(int x) {
    switch (x) {
    case 'AB': return u'\u00e9';
    default: return '\xff';
    }
}
int main(void) {
    if ('A' + 200 != 265 || 'AB' != 0x4142) return 1;
    if ('\xff' != HIGH_BYTE || '\377' != HIGH_BYTE || initial_byte != HIGH_BYTE) return 2;
    if ('é' != 0xc3a9 || '\u00e9' != 'é' || '😀' != -257976192) return 3;
    if (u'é' != 233 || u'\u00e9' != 233 || L'é' != 233) return 4;
    if (U'\U0001f600' != 128512 || U'😀' != 128512) return 5;
    if (latin != 233 || emoji != 128512 || initial_latin != 233) return 6;
    if (character_value(pair) != 233 || character_value(0) != HIGH_BYTE) return 7;
    if (sizeof(u'A') != 2 || sizeof(L'A') != __SIZEOF_WCHAR_T__) return 8;
    if (U'\xffffffff' < 0 || u'\xffff' + 1 != 65536) return 9;
    return 0;
}
'''


def _compile(source, target, mode, monkeypatch):
    ast = make_c_parser().parse(preprocess(source, target_triple=target))
    ctx = PassContext(target_triple=target)
    if mode == "ssa":
        SSABootstrapPass().run(ast, ctx)
        assert set(ctx.ssa_functions) == {"character_value", "main"}
    codegen = CCodeGenerator(pass_ctx=ctx)
    codegen.set_target_text(target, "")
    lowered = set()
    original = codegen._lower_ssa_function

    def record_lowering(name, return_type):
        result = original(name, return_type)
        if result:
            lowered.add(name)
        return result

    monkeypatch.setattr(codegen, "_lower_ssa_function", record_lowering)
    codegen.generate_code(ast)
    if mode == "ssa":
        assert lowered == {"character_value", "main"}, "SSA unexpectedly fell back"
    return emit_owned_object(str(codegen.module), target)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("mode", ["ast", "ssa"])
def test_character_values_types_and_owned_execution(target, mode, tmp_path, monkeypatch):
    """All targets emit objects; only this host's Linux/x86 object is executed."""
    obj = tmp_path / "characters.o"
    binary = tmp_path / "characters"
    start = tmp_path / "start.s"
    start.write_text(".intel_syntax noprefix\n.text\n.globl _start\n_start:\n call main\n mov edi, eax\n mov eax, 60\n syscall\n")
    executable = target == TARGETS[0] and sys.platform == "linux" and platform.machine() == "x86_64"
    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", _forbid_process)
        obj.write_bytes(_compile(CHARACTERS, target, mode, guard))
        assert obj.stat().st_size > 0
        if executable:
            link_inputs(target=target, output=str(binary), objects=[str(obj)], assembly=[str(start)])
    if executable:
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result
        assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("literal", ["''", r"'\q'", r"'\x'", r"'\u0041'", r"U'\U00110000'", r"u'\ud800'", r"u'\U0001f600'", "L'AB'", r"'\x100'", "'\udce9'"])
def test_invalid_character_diagnostics_agree(literal):
    from pcc.frontends.c.ast.c_ast import Constant

    with pytest.raises(_CppExprError):
        _eval_cpp_expr(literal)
    with pytest.raises(ValueError):
        CCodeGenerator().codegen_Constant(Constant("char", literal))
    with pytest.raises(ValueError):
        SSABuilder()._parse_char_constant(literal)


def test_original_invalid_source_encoding_is_rejected():
    # Upstream's exact ISO-8859-1 fixture is a C++11 diagnostic test. This
    # checks PCC's UTF-8 input diagnostic, not upstream C++ warning parity.
    path = Path(__file__).resolve().parents[2] / "projects/clang-c-tests/char-literal-encoding-error.c"
    source = path.read_text(encoding="utf-8", errors="surrogateescape")
    ast = make_c_parser().parse(preprocess(source))
    with pytest.raises(ValueError, match="illegal character encoding"):
        CCodeGenerator().generate_code(ast)
