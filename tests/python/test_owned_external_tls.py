"""External TLS metadata must survive C, indexed IR and owned object emission."""

import pytest

from pcc.backend.owned_object_emit import emit_owned_object


TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
           "x86_64-pc-windows-msvc")


@pytest.mark.parametrize("directive, expected_type", [(".globl unused", 0), (".type unused, @object", 1), (".type unused, @tls_object", 6)])
def test_x86_external_declaration_without_relocation_retains_metadata(directive, expected_type):
    from pcc.backend.x86_64_asm_driver import assemble_file

    obj = assemble_file(".intel_syntax noprefix\n" + directive + "\n")
    symbol = next(symbol for symbol in obj.symbols if symbol.name == "unused")
    assert symbol.section_index == 0 and symbol.binding == 1
    assert symbol.type == expected_type


def test_x86_rejects_empty_type_declaration_symbol():
    from pcc.backend.x86_64_asm_driver import assemble_file
    from pcc.backend.x86_64_encode import X86EncodeError

    with pytest.raises(X86EncodeError, match="bad .type directive"):
        assemble_file(".intel_syntax noprefix\n.type , @tls_object\n")


def _read_ir(target, name="shared_tls"):
    return f'''target triple = "{target}"
@{name} = external thread_local global i64, align 8
define i64 @read_tls() {{
entry:
  %value = load i64, ptr @{name}, align 8
  ret i64 %value
}}
'''


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("indexed", [False, True])
def test_external_tls_object_is_undefined_typed_reference(tmp_path, target, indexed):
    if indexed:
        from pcc.backend.self_backend_parse import parse_self_backend_module
        from pcc.backend.self_backend_kernel import get_indexed_function_kernel
        from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file, decode_indexed_module_file
        from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
        path = str(tmp_path / "unit.pcir")
        source_module = parse_self_backend_module(_read_ir(target))
        for function in source_module.functions:
            get_indexed_function_kernel(function)
        encode_indexed_module_file(path, source_module)
        module = decode_indexed_module_file(path)
        declaration = next(g for g in module.globals_ if g.name == "shared_tls")
        assert declaration.tls_model == "default" and declaration.initializer == ""
        data = encode_assembly_object(emit_indexed_assembly(module), target)
    else:
        data = emit_owned_object(_read_ir(target), target)
    if "windows" in target:
        from pcc.backend.coff_x86_64 import parse_object, SECREL
        obj = parse_object(data)
        index = next(i for i, s in enumerate(obj.symbols) if s.name == "shared_tls")
        assert obj.symbols[index].section == 0
        assert any(r.symbol == index and r.kind == SECREL for s in obj.sections for r in s.relocations)
        assert not any(s.name.startswith(".tls") for s in obj.sections)
    else:
        from pcc.backend.elf_x86_64 import parse_relocatable, STT_TLS, SHF_TLS
        obj = parse_relocatable(data)
        index = next(i for i, s in enumerate(obj.symbols) if s.name == "shared_tls")
        assert obj.symbols[index].section_index == 0
        assert obj.symbols[index].type == STT_TLS
        kinds = {r.type for s in obj.sections for r in s.relocations if r.symbol_index == index}
        assert kinds == ({541, 542} if target.startswith("aarch64") else {22})
        assert not any(s.flags & SHF_TLS for s in obj.sections)
        assert not any("tls_refs" in s.name for s in obj.sections)


@pytest.mark.parametrize("target", TARGETS)
def test_external_tls_definition_is_selected_from_owned_archive(target):
    from pcc.backend.ar_writer import write_archive
    definition = emit_owned_object(
        f'target triple = "{target}"\n@shared_tls = thread_local global i64 37, align 8\n', target)
    reference = emit_owned_object(_read_ir(target), target)
    archive = write_archive([("tls-definition.o", definition)])
    if "windows" in target:
        from pcc.backend.coff_x86_64 import parse_object
        from pcc.backend.pe_x86_64 import link_executable
        image = link_executable([parse_object(reference)], archives=[archive], entry="read_tls")
        assert image[:2] == b"MZ"
    else:
        from pcc.backend.elf_x86_64 import parse_relocatable, link_static_executable, parse_static_executable
        image = link_static_executable([parse_relocatable(reference)], archives=[archive], entry="read_tls")
        assert parse_static_executable(image)["tls_segments"] == 1


def _c_ir(source, target):
    from pcc.frontends.c.codegen.c_codegen import CCodeGenerator
    from pcc.frontends.c.parse.c_parser import CParser
    generator = CCodeGenerator()
    generator.module.triple = target
    generator.generate_code(CParser().parse(source))
    return str(generator.module)


@pytest.mark.parametrize("target", TARGETS)
def test_c_storage_classes_reach_tls_object_emission(target):
    source = '''
extern _Thread_local long long shared_tls;
_Thread_local long long own_tls = 37;
long long read_tls(void) {
    extern _Thread_local long long other_tls;
    static _Thread_local long long local_tls = 5;
    local_tls += 1;
    return shared_tls + own_tls + other_tls + local_tls;
}
'''
    text = _c_ir(source, target)
    for name in ("shared_tls", "other_tls"):
        assert f"@{name} = external thread_local global i64" in text
    assert "@own_tls = thread_local global i64 37" in text
    assert "internal thread_local global i64 5" in text
    # Run the actual frontend output through the object producer as well:
    # accepting a storage keyword without target TLS lowering is insufficient.
    assert emit_owned_object(text, target)


@pytest.mark.parametrize("source", [
    "_Thread_local int value; extern int value;",
    "int value; extern _Thread_local int value;",
    "_Thread_local int value; int read(void) { extern int value; return value; }",
    "int read(void) { _Thread_local int value; return value; }",
])
def test_c_rejects_conflicting_or_automatic_tls_declarations(source):
    from pcc.frontends.c.codegen.c_codegen import SemanticError
    from pcc.frontends.c.codegen.c_declaration_state import CodegenError
    with pytest.raises((SemanticError, CodegenError), match="thread-local"):
        _c_ir(source, TARGETS[0])
