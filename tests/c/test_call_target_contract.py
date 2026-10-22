"""C calls retain their callable signature and reject non-callable targets."""
import re

import pytest

from pcc.frontends.c.codegen.c_codegen import CCodeGenerator, SemanticError
from pcc.frontends.c.parse import make_c_parser


POINTER_PROGRAM = '''long next(long value) { return value + 1; }
long local_pointer(long value) {
    long (*callback)(long) = next;
    return callback(value);
}
long argument_pointer(long (*callback)(long), long value) {
    return callback(value);
}
struct Callbacks { long (*callback)(long); };
long member_pointer(struct Callbacks *callbacks, long value) {
    return callbacks->callback(value);
}
int main(void) {
    struct Callbacks callbacks = { next };
    if (local_pointer(41) != 42) return 1;
    if (argument_pointer(next, 41) != 42) return 2;
    if (member_pointer(&callbacks, 41) != 42) return 3;
    return 0;
}
'''


def _generate(source):
    generator = CCodeGenerator()
    generator.set_target_text("arm64-apple-darwin", "")
    generator.codegen(make_c_parser().parse(source))
    return str(generator.module)


@pytest.mark.parametrize("source", (
    "int main(void) { int value = 41; return value(0); }",
    "int main(void) { return (41)(0); }",
    "struct Holder { int value; }; int main(void) { struct Holder h = {41}; return h.value(0); }",
    "int next(int value) { return value + 1; } int main(void) { return next(); }",
    "int next(int value) { return value + 1; } int main(void) { int (*callback)(int) = next; return callback(); }",
    "int next(int value) { return value + 1; } struct Holder { int (*callback)(int); }; int main(void) { struct Holder h = {next}; return h.callback(1, 2); }",
))
def test_invalid_call_target_or_arity_is_an_explicit_diagnostic(source):
    with pytest.raises(SemanticError, match="call"):
        _generate(source)


def test_valid_pointer_forms_keep_long_argument_and_result():
    text = _generate(POINTER_PROGRAM)
    for name in ("local_pointer", "argument_pointer", "member_pointer"):
        found = re.search(r"^define[^\n]*@" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
        assert found is not None, name
        assert re.search(r"call i64 \(i64\)", found.group(1)), found.group(1)


def test_void_pointer_call_keeps_the_real_call():
    text = _generate("void consume(int value) {} int main(void) { void (*callback)(int) = consume; callback(41); return 0; }")
    assert re.search(r"call void \(i32\)", text), text


def test_variadic_and_unprototyped_calls_keep_default_promotions():
    text = _generate("int variadic(int first, ...); int oldstyle(); int main(void) { return variadic(1, 2, 3) + oldstyle(4, 5); }")
    assert "@variadic(i32 1, i32 2, i32 3)" in text, text
    assert "bitcast ptr @oldstyle to ptr" in text, text
    assert re.search(r"call i32 \(i32, i32\) %[^\s(]+\(i32 4, i32 5\)", text), text
