"""C99 integer headers and composed global-address constants use owned emission."""

import platform
import subprocess
import sys

import pytest

from pcc.backend import BackendUnavailable, self_backend_parse as parser
from pcc.backend.owned_elf_link import link_inputs
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


TARGETS = (
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
    "arm64-apple-darwin", "x86_64-pc-windows-msvc",
)

STDINT_SOURCE = r"""
#include <stdint.h>
#include <stdint.h>
#if INT8_MIN != -128 || INT16_MAX != 32767 || UINT32_MAX != 4294967295U
#error invalid C99 preprocessor limits
#endif
#if INT64_C(9223372036854775807) != INT64_MAX || UINT64_C(18446744073709551615) != UINT64_MAX
#error invalid C99 preprocessor constants
#endif
int main(void) {
"""
STDINT_SOURCE += r"""
    if (INT8_MIN != (-127 - 1)) return 1;
    if (INT8_MAX != 127) return 2;
    if (UINT8_MAX != 255) return 3;
    if (INT16_MIN != (-32767 - 1)) return 1;
    if (INT16_MAX != 32767) return 2;
    if (UINT16_MAX != 65535) return 3;
    if (INT32_MIN != (-2147483647 - 1)) return 1;
    if (INT32_MAX != 2147483647) return 2;
    if (UINT32_MAX != 4294967295U) return 3;
    if (INT64_MIN != (-9223372036854775807LL - 1)) return 1;
    if (INT64_MAX != 9223372036854775807LL) return 2;
    if (UINT64_MAX != 18446744073709551615ULL) return 3;
    if (INT_LEAST8_MIN != (-127 - 1)) return 1;
    if (INT_LEAST8_MAX != 127) return 2;
    if (UINT_LEAST8_MAX != 255) return 3;
    if (INT_LEAST16_MIN != (-32767 - 1)) return 1;
    if (INT_LEAST16_MAX != 32767) return 2;
    if (UINT_LEAST16_MAX != 65535) return 3;
    if (INT_LEAST32_MIN != (-2147483647 - 1)) return 1;
    if (INT_LEAST32_MAX != 2147483647) return 2;
    if (UINT_LEAST32_MAX != 4294967295U) return 3;
    if (INT_LEAST64_MIN != (-9223372036854775807LL - 1)) return 1;
    if (INT_LEAST64_MAX != 9223372036854775807LL) return 2;
    if (UINT_LEAST64_MAX != 18446744073709551615ULL) return 3;
    if (INT_FAST8_MIN != (-127 - 1)) return 1;
    if (INT_FAST8_MAX != 127) return 2;
    if (UINT_FAST8_MAX != 255) return 3;
    if (INT_FAST16_MIN != (-32767 - 1)) return 1;
    if (INT_FAST16_MAX != 32767) return 2;
    if (UINT_FAST16_MAX != 65535) return 3;
    if (INT_FAST32_MIN != (-2147483647 - 1)) return 1;
    if (INT_FAST32_MAX != 2147483647) return 2;
    if (UINT_FAST32_MAX != 4294967295U) return 3;
    if (INT_FAST64_MIN != (-9223372036854775807LL - 1)) return 1;
    if (INT_FAST64_MAX != 9223372036854775807LL) return 2;
    if (UINT_FAST64_MAX != 18446744073709551615ULL) return 3;
    if (INTPTR_MIN != INT64_MIN || INTPTR_MAX != INT64_MAX || UINTPTR_MAX != UINT64_MAX) return 4;
    if (INTMAX_MIN != INT64_MIN || INTMAX_MAX != INT64_MAX || UINTMAX_MAX != UINT64_MAX) return 5;
    if (PTRDIFF_MIN != INT64_MIN || PTRDIFF_MAX != INT64_MAX || SIZE_MAX != UINT64_MAX) return 6;
    if (SIG_ATOMIC_MIN != INT32_MIN || SIG_ATOMIC_MAX != INT32_MAX) return 7;
    if (WINT_MIN != INT32_MIN || WINT_MAX != INT32_MAX) return 8;
    if ((wchar_t)WCHAR_MIN != WCHAR_MIN || (wchar_t)WCHAR_MAX != WCHAR_MAX) return 9;
    if (_Generic(INT8_C(1), int: 1, default: 0) != 1 || _Generic(UINT8_C(1), int: 1, default: 0) != 1) return 10;
    if (_Generic(INT16_C(1), int: 1, default: 0) != 1 || _Generic(UINT16_C(1), int: 1, default: 0) != 1) return 11;
    if (_Generic(INT32_C(1), int: 1, default: 0) != 1 || _Generic(UINT32_C(1), unsigned int: 1, default: 0) != 1) return 12;
    if (_Generic(INT64_C(1), int64_t: 1, default: 0) != 1 || _Generic(UINT64_C(1), uint64_t: 1, default: 0) != 1) return 13;
    if (_Generic(INTMAX_C(1), intmax_t: 1, default: 0) != 1 || _Generic(UINTMAX_C(1), uintmax_t: 1, default: 0) != 1) return 14;
    if (sizeof(INT64_MIN) != 8 || sizeof(UINT64_MAX) != 8 || sizeof(INT8_MIN) != sizeof(int)) return 15;
    return 0;
}
"""


# Constant-construction suffixes must match target typedefs at compile time.
STDINT_SOURCE = STDINT_SOURCE.replace("int main(void)", r"""
_Static_assert(_Generic(INT64_C(1), int64_t: 1, default: 0), "int64 constant type");
_Static_assert(_Generic(UINT64_C(1), uint64_t: 1, default: 0), "uint64 constant type");
_Static_assert(_Generic(INTMAX_C(1), intmax_t: 1, default: 0), "intmax constant type");
_Static_assert(_Generic(UINTMAX_C(1), uintmax_t: 1, default: 0), "uintmax constant type");
int main(void)""")

GLOBAL_GEP_SOURCE = r"""
struct Row { char tag; int cells[3]; };
static struct Row rows[2] = {{1, {11, 12, 13}}, {2, {21, 22, 23}}};
static int *last = &rows[1].cells[2];
static int *first = &rows[0].cells[0];
static int *middle = &rows[1].cells[1];
int main(void) { return *last != 23 || *first != 11 || *middle != 22; }
"""


def _compile(source, target):
    evaluator = CEvaluator(target_triple=target, backend="self")
    unit = TranslationUnit("constant_contract.c", "constant_contract.c", source)
    compiled = evaluator.compile_translation_units(
        [unit], use_system_cpp=False, frontend_opt_level=0,
    )
    return emit_owned_object(compiled[0][1], target)


@pytest.mark.parametrize("source", [STDINT_SOURCE, GLOBAL_GEP_SOURCE], ids=["stdint", "global_gep"])
@pytest.mark.parametrize("target", TARGETS)
def test_c99_constants_emit_owned_target_objects(monkeypatch, source, target):
    def forbidden(*args, **kwargs):
        raise AssertionError("owned compilation attempted an external process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert _compile(source, target)


# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
@pytest.mark.parametrize("source", [STDINT_SOURCE, GLOBAL_GEP_SOURCE], ids=["stdint", "global_gep"])
def test_c99_constants_execute_owned_linux(tmp_path, monkeypatch, source):
    target = TARGETS[0]
    obj = tmp_path / "control.o"
    start = tmp_path / "start.s"
    binary = tmp_path / "control"
    start.write_text(".intel_syntax noprefix\n.text\n.globl _start\n_start:\n  call main\n  mov edi, eax\n  mov eax, 60\n  syscall\n")
    def forbidden(*args, **kwargs):
        raise AssertionError("owned compilation attempted an external process")
    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", forbidden)
        obj.write_bytes(_compile(source, target))
        link_inputs(target=target, output=str(binary), objects=[str(obj)], assembly=[str(start)])
    result = subprocess.run([str(binary)], capture_output=True, timeout=10)
    assert result.returncode == 0, result
    assert result.stdout == result.stderr == b""


def test_nested_constant_gep_keeps_layout_and_signed_addends():
    expression = "getelementptr (i8, ptr bitcast (ptr getelementptr (%T, ptr @storage, i32 0, i32 1) to ptr), i64 -1)"
    a = parser.parse_self_backend_module('target triple = "x86_64-unknown-linux-gnu"\n' + "%T = type { i8, i64 }\n@storage = global %T zeroinitializer\n")
    b = parser.parse_self_backend_module('target triple = "x86_64-unknown-linux-gnu"\n' + "%T = type { i16, i16 }\n@storage = global %T zeroinitializer\n")
    assert parser.parse_constant_gep(expression, type_context=a.type_context) == ("storage", 7)
    assert parser.parse_constant_gep(expression, type_context=b.type_context) == ("storage", 1)
    with pytest.raises(BackendUnavailable, match="global pointer base"):
        parser.parse_constant_gep("getelementptr (i8, ptr %local, i64 1)")
