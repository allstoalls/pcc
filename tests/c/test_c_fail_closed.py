import pytest

from pcc.codegen.c_codegen import LLVMCodeGenerator, SemanticError
from pcc.parse.c_parser import CParser


def _generate(source: str) -> str:
    generator = LLVMCodeGenerator()
    generator.generate_code(CParser().parse(source))
    return str(generator.module)


def test_static_assert_rejects_nonconstant_condition_instead_of_disappearing():
    source = """
        int runtime_value(void) { return 1; }
        _Static_assert(runtime_value(), "must be constant");
        int main(void) { return 0; }
    """

    with pytest.raises(SemanticError, match="_Static_assert condition"):
        _generate(source)


def test_global_array_rejects_nonconstant_element_instead_of_zero_filling():
    source = """
        int runtime_value(void) { return 1; }
        static int values[1] = {runtime_value()};
        int main(void) { return values[0]; }
    """

    with pytest.raises(ValueError, match="compile-time constant"):
        _generate(source)


def test_static_definition_settles_an_implicit_use_on_one_symbol():
    """A `static` helper used before its definition must keep call and body on
    the same symbol.

    pcc's own C inliner lowers a wrapper body at an earlier call site, so a
    `static` helper defined further down the file is used before its
    declaration is reached. Settling that fabricated record by renaming the
    definition to the internal symbol left the early calls pointing at an
    undefined external name (py_obj.c's
    pcc_gc_store_plan_commit_locked_impl made every pcc-C runtime archive
    unlinkable).
    """
    module = _generate(
        """
        int use(void) { return helper(1); }
        static int helper(int x) { return x + 1; }
        int main(void) { return use() == 2 ? 0 : 1; }
        """
    )

    assert "define internal i32 @helper(" in module
    assert "call i32 (i32) @helper(" in module
    assert "declare i32 @helper(" not in module


def test_extern_declaration_before_static_definition_conflicts():
    """An explicit declaration settles the name; a later `static` conflicts.

    Only a fabricated use may be settled. Once the source has declared the
    function with external linkage, a `static` definition is the same
    constraint violation cc reports.
    """
    with pytest.raises(SemanticError, match="conflicting linkage"):
        _generate(
            """
            int use(void) { return helper(1); }
            extern int helper(int);
            static int helper(int x) { return x + 1; }
            int main(void) { return use(); }
            """
        )


def test_static_definition_after_extern_declaration_conflicts():
    with pytest.raises(SemanticError, match="conflicting linkage"):
        _generate(
            """
            extern int helper(int);
            static int helper(int x) { return x + 1; }
            int main(void) { return helper(0); }
            """
        )
