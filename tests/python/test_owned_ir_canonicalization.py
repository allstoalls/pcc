"""Semantic boundaries for the owned runtime IR's redundant representations."""

import pytest

from pcc.native_ir.instsimplify import simplify_module_text


@pytest.mark.parametrize("predicate,constant,inverted", [
    ("ne", "0", False), ("eq", "1", False),
    ("eq", "0", True), ("ne", "1", True),
])
def test_boolean_extension_comparison_recovers_original_condition(predicate, constant, inverted):
    source = f'''define i1 @check(i1 %condition) {{
entry:
  %wide = zext i1 %condition to i64
  %checked = icmp {predicate} i64 %wide, {constant}
  ret i1 %checked
}}
'''
    result, changed = simplify_module_text(source)
    assert changed
    assert "icmp " not in result
    if inverted:
        assert "xor i1 %condition, true" in result
    else:
        assert "ret i1 %condition" in result


@pytest.mark.parametrize("producer,constant", [
    ("sext i1 %condition to i64", "1"),
    ("zext i8 %byte to i64", "0"),
    ("zext i1 %condition to i64", "2"),
])
def test_boolean_recovery_keeps_other_widths_extensions_and_constants(producer, constant):
    source = f'''define i1 @check(i1 %condition, i8 %byte) {{
entry:
  %wide = {producer}
  %checked = icmp eq i64 %wide, {constant}
  ret i1 %checked
}}
'''
    result, changed = simplify_module_text(source)
    assert not changed
    assert result == source


def test_pointer_identity_replacement_preserves_other_names_and_inline_asm():
    source = '''declare ptr @opaque(ptr)
define ptr @forward(ptr %pointer) {
entry:
  %cast = bitcast ptr %pointer to ptr
  %cast.extra = call ptr @opaque(ptr %cast)
  call void asm sideeffect "# %cast", ""()
  ; preserve %cast in this comment
  ret ptr %cast.extra
}
'''
    result, changed = simplify_module_text(source)
    assert changed
    assert "bitcast ptr" not in result
    assert "%cast.extra = call ptr @opaque(ptr %pointer)" in result
    assert 'asm sideeffect "# %cast", ""()' in result
    assert "; preserve %cast in this comment" in result
    assert "ret ptr %cast.extra" in result


def test_long_pointer_identity_chain_has_no_dangling_replacement():
    lines = ["define ptr @forward(ptr %pointer) {", "entry:"]
    previous = "%pointer"
    for index in range(128):
        current = "%cast" + str(index)
        lines.append(f"  {current} = bitcast ptr {previous} to ptr")
        previous = current
    lines.extend([f"  ret ptr {previous}", "}", ""])
    result, changed = simplify_module_text("\n".join(lines))
    assert changed
    assert "bitcast" not in result
    assert "%cast" not in result
    assert "ret ptr %pointer" in result


@pytest.mark.parametrize("predicate,inverse", [
    ("eq", "ne"), ("ne", "eq"), ("slt", "sge"), ("sle", "sgt"),
    ("sgt", "sle"), ("sge", "slt"), ("ult", "uge"), ("ule", "ugt"),
    ("ugt", "ule"), ("uge", "ult"),
])
def test_integer_comparison_negation_uses_inverse_predicate(predicate, inverse):
    source = f'''define i1 @check(i64 %left, i64 %right) {{
entry:
  %comparison = icmp {predicate} i64 %left, %right
  %inverse = xor i1 %comparison, true
  ret i1 %inverse
}}
'''
    result, changed = simplify_module_text(source)
    assert changed
    assert f"%inverse = icmp {inverse} i64 %left, %right" in result
    assert "xor i1" not in result


def test_comparison_negation_keeps_other_users_of_original_comparison():
    source = '''define i1 @check(i64 %left, i64 %right, i1 %choose) {
entry:
  %comparison = icmp eq i64 %left, %right
  %inverse = xor i1 true, %comparison
  %result = select i1 %choose, i1 %comparison, i1 %inverse
  ret i1 %result
}
'''
    result, _ = simplify_module_text(source)
    assert "%comparison = icmp eq i64 %left, %right" in result
    assert "%inverse = icmp ne i64 %left, %right" in result
