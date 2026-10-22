"""Regression: an unused extern declaration that defines a tag must survive.

``extern const struct T { double a; } g;`` does two things at once — it
declares ``g`` and it defines ``struct T``. ``ElimAvailExternPass`` used to
drop the whole declaration whenever ``g`` was not referenced in the same
translation unit, taking the tag body with it. Every later ``struct T`` then
resolved to an opaque identified type, so the object it typed was unsized and
LLVM aborted at object emission ("Cannot getTypeInfo() on a type that is
unsized!") — and before that point the object's initializer had already been
dropped to ``zeroinitializer``, which is silently wrong data rather than a
crash.

musl's ``exp_data.h`` and ``pow_data.h`` are written exactly this way, so the
vendored math tree carried a header split as a workaround
(BUG-P1-CC-EMBEDDED-TAG-IN-EXTERN-DECL-UNSIZED).
"""

import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.parse import make_c_parser
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ipo_boundary import ElimAvailExternPass


def _decl_names(ast):
    return [ext.name for ext in ast.ext if getattr(ext, "name", None)]


def _run_pass(source):
    ast = make_c_parser().parse(source)
    ElimAvailExternPass().run(ast, PassContext())
    return ast


def test_unused_extern_that_defines_a_tag_is_kept():
    ast = _run_pass(
        "extern const struct T { double a; } g;\n"
        "const struct T g = { 1.0 };\n"
    )
    assert _decl_names(ast).count("g") == 2, (
        "the extern declaration carries the only definition of struct T; "
        "dropping it leaves the tag opaque"
    )


def test_unused_extern_that_defines_an_enum_is_kept():
    ast = _run_pass(
        "extern enum E { A = 1, B = 7 } e;\n"
        "enum E e = B;\n"
    )
    assert _decl_names(ast).count("e") == 2


def test_plain_unused_extern_is_still_removed():
    """The pass must keep doing its job for declarations with no tag body."""
    ast = _run_pass("extern int unused_thing;\nint used = 1;\n")
    assert "unused_thing" not in _decl_names(ast)


def _ir_for(source):
    units = CEvaluator().compile_translation_units(
        [TranslationUnit("tagbody.c", "", source)], use_compile_cache=False
    )
    return units[0][1]


def test_the_object_is_sized_and_keeps_its_initializer():
    # No function in the unit reads `g`, which is what made the pass drop the
    # declaration. The object must still come out sized and initialized.
    ir_text = _ir_for(
        "extern const struct T { double a; double poly[2]; } g;\n"
        "const struct T g = { 1.0, { 2.0, 3.0 } };\n"
        "int unrelated(void) { return 1; }\n"
    )
    struct_lines = [
        line for line in ir_text.splitlines()
        if line.startswith("%") and "_struct_" in line and "_T " in line
    ]
    assert struct_lines, ir_text
    assert "type opaque" not in struct_lines[0], struct_lines[0]
    global_line = next(line for line in ir_text.splitlines() if line.startswith("@g "))
    # The values themselves are checked by the runtime test below; the IR only
    # has to show that an initializer survived at all. (This path prints
    # doubles as hex bit patterns, so don't match on decimal spellings.)
    assert "zeroinitializer" not in global_line, global_line


def test_the_values_are_readable_at_runtime():
    assert CEvaluator().evaluate(
        "extern const struct T { double a; double poly[2]; } g;\n"
        "const struct T g = { 1.0, { 2.0, 3.0 } };\n"
        "int main(void) { return g.poly[1] == 3.0 && g.a == 1.0 ? 0 : 1; }\n",
        optimize=False,
    ) == 0
