"""Pure formatting controls; no compiler, object loading, linking, or native run."""

import ast
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]


def _definition(path, name):
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    return next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _pure_function(path, name):
    definition = _definition(path, name)
    namespace = {}
    code = compile(ast.Module(body=[definition], type_ignores=[]), path, "exec")
    exec(code, namespace)
    return namespace[name]


@pytest.mark.parametrize("bits", [21, 26, 32, 64])
def test_signed_failure_reports_actual_value_and_bits(bits):
    signed = _pure_function("pcc/backend/elf_aarch64_relocations.py", "_signed")
    limit = 1 << (bits - 1)
    for value in (-limit, -1, 0, limit - 1):
        assert signed(value, bits) == value & ((1 << bits) - 1)
    for value in (-limit - 1, limit):
        with pytest.raises(ValueError) as caught:
            signed(value, bits)
        assert str(caught.value) == (
            "AArch64 relocation out of range: " + str(value)
            + "; signed_bits=" + str(bits)
        )


@pytest.mark.parametrize("rtype,units", [
    (282, "4-byte-instruction-words"), (283, "4-byte-instruction-words"),
    (275, "4096-byte-pages"), (311, "4096-byte-pages"),
    (541, "4096-byte-pages"), (260, "bytes"), (261, "bytes"), (274, "bytes"),
    (257, "not-signed-pcrel"), (277, "not-signed-pcrel"),
])
def test_error_context_preserves_distinct_resident_values(rtype, units):
    context = _pure_function("pcc/backend/elf_x86_64.py",
                             "_aarch64_relocation_error_context")
    result = context(7, 3, ".text", 24, rtype, 11, "reference", "definition",
                     0x7000, -8, 0x9000, 0x4018, 0x1000, 0x4000, 96, 0xA000)
    assert result == (
        "; rtype=" + str(rtype) + "; relocation_units=" + units
        + "; object_index=7; section_index=3; section='.text'"
        + "; relocation_offset=24; symbol_index=11; symbol='reference'"
        + "; resolved_symbol='definition'; S=28672; A=-8; target_base=36864"
        + "; effective_target=36856; P=16408; section_file_offset=4096"
        + "; section_address=16384; section_size=96; image_size=40960"
    )
    assert "checked_value=" not in result
    assert "signed_bits=" not in result


def test_error_context_escapes_symbol_and_section_text():
    context = _pure_function("pcc/backend/elf_x86_64.py",
                             "_aarch64_relocation_error_context")
    result = context(0, 1, ".text\n", 0, 283, 1, "ref\n", "def\r",
                     0, 0, 0, 0, 0, 0, 4, 4)
    assert "\n" not in result and "\r" not in result
    assert "section=" + repr(".text\n") in result
    assert "symbol=" + repr("ref\n") in result
    assert "resolved_symbol=" + repr("def\r") in result


def test_error_context_call_stays_in_original_exception_handler():
    link = _definition("pcc/backend/elf_x86_64.py", "_link_static_executable_image")
    calls = [node for node in ast.walk(link) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)
             and node.func.id == "_aarch64_relocation_error_context"]
    assert len(calls) == 2
    assert {ast.unparse(call.args[3]) for call in calls} == {"relocation_offset", "relocation.offset"}
    for call in calls:
        handlers = [node for node in ast.walk(link) if isinstance(node, ast.ExceptHandler)
                    and isinstance(node.type, ast.Name) and node.type.id == "ValueError"
                    and call in list(ast.walk(node))]
        assert len(handlers) == 1
        handler = handlers[0]
        assert len(handler.body) == 1 and isinstance(handler.body[0], ast.Raise)
        assert isinstance(handler.body[0].cause, ast.Name)
        assert handler.body[0].cause.id == handler.name == "exc"
