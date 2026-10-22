"""Target-owned compiler types in already-preprocessed C input.

Compiler-provided types can occur without a header declaration in a .i file.
Keep their spelling compatibility separate from preprocessing ownership, and
reuse the declarations consumed by the owned standard headers.
"""

from pcc.frontends.c.c_abi_layout import (
    c_size_type_name,
    c_target_predefines,
)
from pcc.frontends.c.preprocessor import (
    _CPP_TOKEN_RE,
    preprocess,
)


def _declares_typedef(tokens, name):
    for index, token in enumerate(tokens):
        if token != "typedef":
            continue
        depth = 0
        cursor = index + 1
        while cursor < len(tokens):
            token = tokens[cursor]
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
            elif depth == 0 and token == ";":
                break
            elif depth == 0 and token == name:
                following = cursor + 1
                while following < len(tokens) and tokens[following] == "[":
                    brackets = 1
                    following += 1
                    while following < len(tokens) and brackets:
                        brackets += (tokens[following] == "[") - (tokens[following] == "]")
                        following += 1
                if following < len(tokens) and tokens[following] in (";", ","):
                    return True
            cursor += 1
    return False


def _has_ordinary_declaration(tokens, name):
    """Recognize explicit scalar/enum declarations before adding an extension.

    These compatibility names remain ordinary identifiers in pre-C23 C. In
    particular, a variable named bool/true must not acquire a global typedef
    or enumerator merely because the token occurs in the source.
    """
    scalar_types = {"char", "short", "int", "long", "float", "double", "_Bool",
                    "signed", "unsigned", "void"}
    for index, token in enumerate(tokens):
        if token != name:
            continue
        previous = tokens[index - 1] if index else ""
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        # A type specifier is followed by its declarator (or a qualifier),
        # whereas a declared ordinary name is followed by an initializer,
        # suffix, or delimiter. Do not borrow the previous parameter's type
        # across a comma in ``int argc, wchar_t *argv[]``.
        if following not in (";", ",", "=", "[", "(", ")", "}"):
            continue
        if previous in scalar_types:
            return True
        if previous in (",", "*", "(", "const", "volatile"):
            cursor = index - 1
            while cursor >= 0 and tokens[cursor] not in (";", "{", "}", "("):
                if tokens[cursor] in scalar_types:
                    return True
                cursor -= 1
            if previous == "(" and index >= 2 and tokens[index - 2] in scalar_types:
                return True
        if previous in ("{", ",") and following in ("=", ",", "}"):
            # Only an enclosing enum introduces an enumerator.
            start = index - 1
            while start >= 0 and tokens[start] not in (";", "}"):
                if tokens[start] == "enum":
                    return True
                start -= 1
    return False


def _implicit_keyword_declarations(tokens, target_triple):
    declarations = []
    if "wchar_t" in tokens and not (
        _declares_typedef(tokens, "wchar_t") or _has_ordinary_declaration(tokens, "wchar_t")
    ):
        declarations.append("typedef " + c_target_predefines(target_triple)["__WCHAR_TYPE__"] + " wchar_t;")
    if "bool" in tokens and not (
        _declares_typedef(tokens, "bool") or _has_ordinary_declaration(tokens, "bool")
    ):
        declarations.append("typedef _Bool bool;")
    constants = [name + " = " + value for name, value in (("false", "0"), ("true", "1"))
                 if name in tokens and not _has_ordinary_declaration(tokens, name)]
    if constants:
        declarations.append("enum { " + ", ".join(constants) + " };")
    return declarations


def normalize_builtin_type_compat(source, target_triple=None):
    """Preserve target size/varargs types without an external preprocessor.

    Only the proven ``typeof(sizeof(int))`` spelling is lowered here. Arbitrary
    typeof expressions must retain their operand for semantic analysis rather
    than acquiring a guessed scalar type or losing operand diagnostics.
    """
    matches = list(_CPP_TOKEN_RE.finditer(source))
    tokens = [match.group(0) for match in matches]
    pieces = []
    start = 0
    index = 0
    while index + 6 < len(tokens):
        if (tokens[index] in ("typeof", "__typeof", "__typeof__")
                and tokens[index + 1:index + 7] == ["(", "sizeof", "(", "int", ")", ")"]):
            pieces.append(source[start:matches[index].start()])
            pieces.append(c_size_type_name(target_triple))
            start = matches[index + 6].end()
            index += 7
        else:
            index += 1
    pieces.append(source[start:])
    result = "".join(pieces)
    declarations = _implicit_keyword_declarations(tokens, target_triple)
    if "__builtin_va_list" in tokens and not _declares_typedef(tokens, "__builtin_va_list"):
        declarations.append(preprocess('#include <__pcc_builtin_va_list.h>\n', target_triple=target_triple))
    return "\n".join(declarations) + "\n" + result if declarations else result
