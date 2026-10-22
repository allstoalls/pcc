"""Pure LLVM text contracts shared by both pipeline split paths."""

from __future__ import annotations

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_ir_text


def test_private_symbol_map_and_reference_rewrite_preserve_quoted_text():
    functions = [
        ("helper", "define internal void @helper() {", "", True),
        ("public", "define void @public() {", "", False),
    ]
    rename_map = pipeline_ir_text.private_symbol_rename_map(
        ["@state = private global i64 0"],
        functions,
        "__pccsplit_mod_",
    )
    source = 'call void @helper()\n%v = load i64, ptr @state\n@text = "@helper"'

    assert rename_map == {
        "state": "__pccsplit_mod_state",
        "helper": "__pccsplit_mod_helper",
    }
    assert pipeline_ir_text.rename_llvm_global_refs(source, rename_map) == (
        "call void @__pccsplit_mod_helper()\n"
        "%v = load i64, ptr @__pccsplit_mod_state\n"
        '@text = "@helper"'
    )


def test_definition_lines_become_external_declarations():
    assert pipeline_ir_text.function_declaration_from_define_line(
        "define internal i64 @work(i64 %x) {"
    ) == "declare i64 @work(i64 %x)"
    assert pipeline_ir_text.global_declaration_from_definition_line(
        "@items = private constant [2 x i8] c\"x\\00\""
    ) == "@items = external constant [2 x i8]"


def test_pipeline_facade_reexports_shared_ir_text_helpers():
    assert (
        pipeline._defined_function_name_from_line
        is pipeline_ir_text.defined_function_name_from_line
    )
    assert (
        pipeline._rename_llvm_global_refs
        is pipeline_ir_text.rename_llvm_global_refs
    )
    assert pipeline._find_substring is pipeline_ir_text.find_substring
