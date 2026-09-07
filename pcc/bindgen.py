"""Generate explicit C-ABI declarations using pcc's native C parser.

The initial boundary is preprocessed LP64 C declarations: prototypes, scalar
typedefs and raw pointers. It does not infer ownership, wrap strings, execute
the preprocessor, or emit aggregate-by-value/callback bindings.
"""

from __future__ import annotations

import argparse
import keyword
import platform
import sys
from pathlib import Path

from pcc.ast import c_ast
from pcc.parse.c_parse_driver import CParseDriver
from pcc.parse.plyparser import ParseError


class BindgenError(ValueError):
    """A declaration cannot be represented by the supported extern ABI."""


_SCALARS = {
    "void": "c_void",
    "_Bool": "c_bool",
    "char": "c_int8",
    "signed char": "c_int8",
    "unsigned char": "c_uint8",
    "short": "c_int16",
    "short int": "c_int16",
    "signed short": "c_int16",
    "signed short int": "c_int16",
    "unsigned short": "c_uint16",
    "unsigned short int": "c_uint16",
    "int": "c_int32",
    "signed": "c_int32",
    "signed int": "c_int32",
    "unsigned": "c_uint32",
    "unsigned int": "c_uint32",
    "long": "c_int64",
    "long int": "c_int64",
    "signed long": "c_int64",
    "signed long int": "c_int64",
    "unsigned long": "c_uint64",
    "unsigned long int": "c_uint64",
    "long long": "c_int64",
    "long long int": "c_int64",
    "signed long long": "c_int64",
    "signed long long int": "c_int64",
    "unsigned long long": "c_uint64",
    "unsigned long long int": "c_uint64",
    "float": "c_float",
    "double": "c_double",
}
_TARGETS = (
    "arm64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-unknown-linux-gnu",
)


def _host_target() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in ("arm64", "aarch64", "x86_64"):
        return machine + "-apple-darwin"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-gnu"
    raise BindgenError("host ABI is not supported; specify a supported --target")


def _error(node, message: str) -> BindgenError:
    return BindgenError(str(node.coord or "<header>") + ": " + message)


def _resolve_type(node, typedefs: dict, active: tuple = ()):
    while isinstance(node, c_ast.TypeDecl):
        node = node.type
    if isinstance(node, c_ast.IdentifierType) and len(node.names) == 1:
        name = node.names[0]
        if name in typedefs:
            if name in active:
                raise _error(node, "cyclic typedef: " + name)
            return _resolve_type(typedefs[name], typedefs, active + (name,))
    return node


def _ctype(node, typedefs: dict, *, argument: bool) -> str:
    node = _resolve_type(node, typedefs)
    if isinstance(node, c_ast.IdentifierType):
        name = " ".join(node.names)
        result = _SCALARS.get(name)
        if result is None:
            raise _error(node, "unsupported scalar or unresolved typedef: " + name)
        return result
    if isinstance(node, c_ast.PtrDecl) or (
        argument and isinstance(node, c_ast.ArrayDecl)
    ):
        pointee = _resolve_type(node.type, typedefs)
        if isinstance(pointee, c_ast.FuncDecl):
            raise _error(node, "callback/function-pointer ABI is not yet supported")
        if isinstance(pointee, c_ast.IdentifierType):
            _ctype(pointee, typedefs, argument=False)
        # Raw storage only. In particular char* never silently becomes a
        # Python str or a managed PyObject*; lifetime is the caller's contract.
        return "c_rawptr"
    raise _error(node, "unsupported C ABI type: " + type(node).__name__)


def generate_bindings(
    source: str, *, target: str | None = None, filename: str = "<header>"
) -> str:
    """Return deterministic Python extern declarations, or reject the input.

    Directives are rejected rather than passing through the existing C
    preprocessor's permissive missing-header/preamble policy. A future owned
    preprocessing slice must retain exact ABI typedefs before enabling it.
    """
    target = target or _host_target()
    if target not in _TARGETS:
        raise BindgenError("unsupported target ABI: " + target)
    for number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#"):
            raise BindgenError(
                filename + ":" + str(number) + ": preprocessing directives are "
                "not yet supported; provide expanded declarations"
            )
    try:
        tree = CParseDriver().parse(source, filename=filename)
    except ParseError as exc:
        raise BindgenError(str(exc)) from exc
    typedefs = {}
    declarations = []
    names = set()
    for node in tree.ext:
        if isinstance(node, c_ast.Typedef):
            typedefs[node.name] = node.type
            continue
        if isinstance(node, c_ast.Decl) and node.name is None:
            if isinstance(node.type, (c_ast.Struct, c_ast.Union)):
                continue  # Definition/forward declaration; usable behind a pointer.
        if not isinstance(node, c_ast.Decl):
            raise _error(
                node, "expected a typedef, opaque record or function prototype"
            )
        function = _resolve_type(node.type, typedefs)
        if not isinstance(function, c_ast.FuncDecl):
            raise _error(
                node, "global variables and record values are not yet supported"
            )
        if "static" in node.storage or node.funcspec:
            raise _error(
                node, "static/inline declarations have no supported external binding"
            )
        if function.args is None:
            raise _error(
                node,
                "function needs an explicit prototype; use (void) for no arguments",
            )
        args = []
        variadic = False
        for parameter in function.args.params:
            if isinstance(parameter, c_ast.EllipsisParam):
                variadic = True
                continue
            if isinstance(parameter, c_ast.ID):
                raise _error(
                    parameter, "old-style function parameters are not supported"
                )
            marker = _ctype(parameter.type, typedefs, argument=True)
            args.append(marker)
        if args == ["c_void"] and not variadic:
            args = []
        elif "c_void" in args:
            raise _error(node, "void must be the only parameter")
        if variadic and not args:
            raise _error(node, "variadic prototype requires a fixed parameter")
        result = _ctype(function.type, typedefs, argument=False)
        name = node.name
        if (
            not name.isidentifier()
            or keyword.iskeyword(name)
            or name in _SCALARS.values()
            or name == "extern"
        ):
            raise _error(node, "C name cannot be exported unchanged to Python: " + name)
        if name in names:
            raise _error(node, "duplicate function declaration: " + name)
        names.add(name)
        declarations.append((name, args, result, variadic))
    if not declarations:
        raise BindgenError("no supported function prototypes found")
    markers = set()
    for name, args, result, variadic in declarations:
        markers.update(args)
        markers.add(result)
    lines = [
        '"""Generated by pcc bindgen for ' + target + ".",
        "Raw pointers have no inferred ownership or string conversion.",
        'These extern declarations are consumed by pcc compilation."""',
        "from pcc.extern import extern, " + ", ".join(sorted(markers)),
        "",
    ]
    for name, args, result, variadic in declarations:
        arg_text = ", ".join(args) + ("," if len(args) == 1 else "")
        declaration = (
            name
            + " = extern("
            + repr(name)
            + ", argtypes=("
            + arg_text
            + "), restype="
            + result
        )
        if variadic:
            declaration += ", variadic=True"
        lines.append(declaration + ")")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pcc bindgen", description=__doc__)
    parser.add_argument("header")
    parser.add_argument("-o", "--output", help="new output file (default: stdout)")
    parser.add_argument("--target", choices=_TARGETS)
    args = parser.parse_args(argv)
    try:
        source = Path(args.header).read_text(encoding="utf-8")
        result = generate_bindings(source, target=args.target, filename=args.header)
        if args.output:
            # Generation succeeds before publishing; never overwrite input or
            # another generated/user-authored file implicitly.
            with open(args.output, "x", encoding="utf-8") as output:
                output.write(result)
        else:
            sys.stdout.write(result)
    except (OSError, ValueError, UnicodeError) as exc:
        print("PCC-BINDGEN-001: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
