"""Pointer constant casts preserve addresses, addends and literal words."""
import struct

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.elf_x86_64 import parse_relocatable
from pcc.backend.owned_object_emit import emit_owned_object


CASES = (
    ("bitcast (ptr @target to ptr)", 0, None),
    ("bitcast (ptr getelementptr ([8 x i8], ptr @target, i64 0, i64 3) to ptr)", 3, None),
    ("bitcast (ptr inttoptr (i64 199 to ptr) to ptr)", None, 199),
)


def _program(expression):
    return ('@target = global [8 x i8] c"ABCDEFGH"\n'
            '@table = global [2 x ptr] [ptr ' + expression
            + ', ptr inttoptr (i64 7 to ptr)]\n'
            'define i32 @main() {\nentry:\n  ret i32 0\n}\n')


@pytest.mark.parametrize("expression,addend,literal", CASES)
def test_x86_owned_pointer_cast_object_preserves_relocation_and_words(expression, addend, literal):
    obj = parse_relocatable(emit_owned_object(_program(expression), "x86_64-unknown-linux-gnu"))
    table = next(symbol for symbol in obj.symbols if symbol.name == "table")
    section = obj.sections[table.section_index - 1]
    assert struct.unpack_from("<Q", section.data, table.value + 8)[0] == 7
    relocations = [rel for rel in section.relocations if rel.offset == table.value]
    if literal is not None:
        assert not relocations
        assert struct.unpack_from("<Q", section.data, table.value)[0] == literal
    else:
        assert len(relocations) == 1
        rel = relocations[0]
        assert rel.type == 1 and rel.addend == addend
        assert obj.symbols[rel.symbol_index].name == "target"


@pytest.mark.parametrize("expression,addend,literal", CASES)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "aarch64-unknown-linux-gnu",
                                    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_pointer_cast_constants_reach_all_owned_object_formats(expression, addend, literal, target):
    assert len(emit_owned_object(_program(expression), target)) > 0


def test_invalid_integer_pointer_bitcast_has_a_capability_diagnostic():
    with pytest.raises(BackendUnavailable, match="bitcast"):
        emit_owned_object(_program("bitcast (i64 42 to ptr)"), "x86_64-unknown-linux-gnu")
