"""C assertions use typed constant evaluation, without runtime IR emission."""

import platform
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.backend.owned_elf_link import link_inputs
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator, SemanticError
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.c.parse.c_parser import CParser


TARGETS = (
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
    "arm64-apple-darwin", "x86_64-pc-windows-msvc",
)

CONSTANT_SOURCE = r"""
#include <stddef.h>
#include <stdint.h>
#include "pcc_stdio_abi.h"
enum { E = 7 };
_Static_assert((long long)(signed char)255 == -1, "signed extension");
_Static_assert((unsigned long long)(unsigned char)-1 == 255, "zero extension");
_Static_assert((signed short)65535 == -1, "signed narrowing");
_Static_assert((unsigned short)65537 == 1, "unsigned narrowing");
_Static_assert((unsigned int)-1 == 4294967295U, "same width sign cast");
_Static_assert((unsigned char)255 + 1 == 256, "integer promotion");
_Static_assert((_Bool)256 == 1 && !(_Bool)0, "boolean conversion");
_Static_assert(-1 < 1U == 0, "mixed comparison");
_Static_assert(-1LL < 1U, "wider signed comparison");
_Static_assert((0U - 1U) >> 31 == 1, "unsigned arithmetic and shift");
_Static_assert((1 ? -1 : 1U) > 0, "conditional common unsigned type");
_Static_assert((1 ? -1LL : 1U) < 0, "conditional wider signed type");
_Static_assert((1 ? 7 : 1 / 0) == 7, "unselected division");
_Static_assert(1 || (1 / 0), "short circuit");
_Static_assert(E * 6 == 42 && (int)3.75 == 3, "enum and integer cast");
_Static_assert(sizeof(_Float16) == 2 && _Alignof(_Float16) == 2, "half layout");
_Static_assert(_Generic((uint64_t)0, uint64_t: 1, default: 0), "generic selection");
#if defined(_WIN32)
_Static_assert(sizeof(long) == 4 && (unsigned long)-1 == 4294967295U, "LLP64");
#else
_Static_assert(sizeof(long) == 8 && (unsigned long)-1 == 18446744073709551615ULL, "LP64");
#endif
static long long signed_value = (long long)(signed char)255;
static unsigned long long unsigned_value = (unsigned long long)(unsigned char)-1;
static unsigned int selected_value = (1 ? -1 : 1U);
static unsigned long long offset_value = offsetof(PccOwnedFile, buffer_position);
int main(void) {
    _Static_assert(sizeof(PccOwnedFile) == 64, "block assertion");
    return signed_value != -1 || unsigned_value != 255
        || selected_value != 4294967295U || offset_value != 56;
}
"""


POINTER_CONSTANT_SOURCE = r"""
struct Entry { const char *name; void *tag; };
static struct Entry entries[] = { { "five", (void *)(long)5 }, { "null", (void *)0 } };
static void *selected = 1 ? (void *)5 : (void *)0;
static const char *text = 1 ? "five" : "zero";
static void *narrowed = (void *)(unsigned char)257;
static long roundtrip = (long)(void *)5;
int main(void) {
    return (long)entries[0].tag != 5 || entries[1].tag != 0
        || entries[0].name[0] != 'f' || (long)selected != 5
        || text[0] != 'f' || (long)narrowed != 1 || roundtrip != 5;
}
"""


def _compile_ir(source, target, opt_level=0):
    root = Path(__file__).resolve().parents[2]
    evaluator = CEvaluator(backend="self", target_triple=target)
    units = evaluator.compile_translation_units(
        [TranslationUnit("static_constants.c", "static_constants.c", source)],
        use_system_cpp=False, use_compile_cache=False, frontend_opt_level=opt_level,
        include_dirs=[str(root / "pcc/runtime/include")],
    )
    return units[0][1]


def _compile(source, target):
    return emit_owned_object(_compile_ir(source, target), target)


def _forbidden(*args, **kwargs):
    raise AssertionError("owned constant compilation attempted an external process")


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("source", [CONSTANT_SOURCE, POINTER_CONSTANT_SOURCE], ids=["integer", "pointer"])
def test_assertions_emit_owned_target_objects(monkeypatch, target, source):
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    assert _compile(source, target)


@pytest.mark.parametrize("condition", [
    "runtime_value()", "global_value", "(global_value = 1)", "++global_value",
    '"text"', '(int*)1', '((int*)1 != (int*)0)', '(int)(void*)1',
    "1.0", "1.0 == 1.0", "(int)(1.0 + 2.0)", "(int)1e100",
    "1 / 0", "1 << 32", "2147483647 + 1", "(1, 2)",
    "global_value * 0 + 1", "(global_value - global_value) + 1",
    "1 || runtime_value()", "(0 && runtime_value()) == 0",
    "1 || global_value", "1 ? 1 : global_value", "1 || (global_value = 1)",
    "1 || ++global_value", "1 || 1.0", "(int)-3.75 == -3",
])
@pytest.mark.parametrize("route", ["codegen", "pipeline0", "pipeline2"])
def test_assertions_reject_nonconstant_or_undefined_operands(condition, route):
    source = "int runtime_value(void); int global_value;"
    source += '_Static_assert(' + condition + ', "must be constant");'
    generator = CCodeGenerator()
    with pytest.raises(SemanticError, match="_Static_assert condition"):
        if route == "codegen":
            generator.generate_code(CParser().parse(source))
        else:
            _compile_ir(source, TARGETS[0], opt_level=int(route[-1]))


def test_false_assertion_keeps_its_diagnostic():
    generator = CCodeGenerator()
    with pytest.raises(SemanticError, match="_Static_assert failed: signedness matters"):
        generator.generate_code(CParser().parse(
            '_Static_assert(-1 < 1U, "signedness matters");'
        ))


def test_block_assertion_does_not_emit_runtime_operations():
    generator = CCodeGenerator()
    generator.generate_code(CParser().parse(
        'int main(void) { _Static_assert((long long)(signed char)255 == -1, "ok"); return 0; }'
    ))
    text = str(generator.module)
    assert "icmp" not in text and "sext" not in text and "trunc" not in text


# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
@pytest.mark.parametrize("source", [CONSTANT_SOURCE, POINTER_CONSTANT_SOURCE], ids=["integer", "pointer"])
def test_constant_initializers_execute_owned_linux(tmp_path, monkeypatch, source):
    target = TARGETS[0]
    obj, start, binary = (tmp_path / name for name in ("control.o", "start.s", "control"))
    start.write_text(".intel_syntax noprefix\n.text\n.globl _start\n_start:\n  call main\n  mov edi, eax\n  mov eax, 60\n  syscall\n")
    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", _forbidden)
        obj.write_bytes(_compile(source, target))
        link_inputs(target=target, output=str(binary), objects=[str(obj)], assembly=[str(start)])
    result = subprocess.run([str(binary)], capture_output=True, timeout=10)
    assert result.returncode == 0, result
    assert result.stdout == result.stderr == b""


# Preserved verbatim from test_clang_compat.py; executable half lowering is a
# separate backend capability, so this checks the original frontend boundary.
FLOAT16_SOURCE = r"""
        _Static_assert(sizeof(_Float16) == 2, "_Float16 must use a 16-bit ABI");
        _Static_assert(_Alignof(_Float16) == 2, "_Float16 alignment must be 2");

        int main(void) {
            _Float16 half = 1.5f;
            float value = half;
            _Float16 rounded = 1.0f / 3.0f;
            return value > 1.0f && value < 2.0f
                && (float)rounded > 0.333f && (float)rounded < 0.334f
                ? 0 : 1;
        }
    """


@pytest.mark.parametrize("target", TARGETS)
def test_original_float16_assertions_reach_typed_ir(monkeypatch, target):
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    text = _compile_ir(FLOAT16_SOURCE, target)
    assert "half" in text and "fptrunc" in text and "fpext" in text


@pytest.mark.parametrize("condition", [
    "1 || (1 / 0)", "(0 && (1 / 0)) == 0", "(1 ? 7 : 1 / 0) == 7",
    "(0 ? 1 / 0 : 7) == 7", "1 || (1 << 32)", "1 || (1, 2)",
    "1 ? 1 : (1, 2)", "(int)3.75 == 3",
])
@pytest.mark.parametrize("opt_level", [0, 2])
def test_integer_constant_constraints_distinguish_unevaluated_operations(condition, opt_level):
    _compile_ir('_Static_assert(' + condition + ', "valid ICE"); int main(void) { return 0; }', TARGETS[0], opt_level=opt_level)
