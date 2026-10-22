"""Host-side signature audit of scaffold declarations against their provider.

Read the real definitions as source. This never imports llvmlite, executes a
provider import hook, or adds reflection to a native compiler. Python call
signatures are checked here; machine ABI/ownership parity is a separate gate.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: str
    required: bool
    default: str | None


@dataclass(frozen=True)
class SignatureIssue:
    method: str
    target: str
    code: str
    detail: str


def definition_signatures(source: str) -> dict[str, tuple[Parameter, ...]]:
    """Return lexical definition identities, preserving parameter kinds."""
    result = {}

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                positional = args.posonlyargs + args.args
                defaults = [None] * (len(positional) - len(args.defaults)) + list(
                    args.defaults
                )
                parameters = []
                for index, (argument, default) in enumerate(zip(positional, defaults)):
                    parameters.append(
                        Parameter(
                            argument.arg,
                            "pos_only" if index < len(args.posonlyargs) else "pos",
                            default is None,
                            (
                                None
                                if default is None
                                else ast.dump(default, include_attributes=False)
                            ),
                        )
                    )
                if args.vararg is not None:
                    parameters.append(Parameter(args.vararg.arg, "*args", False, None))
                for argument, default in zip(args.kwonlyargs, args.kw_defaults):
                    parameters.append(
                        Parameter(
                            argument.arg,
                            "kw_only",
                            default is None,
                            (
                                None
                                if default is None
                                else ast.dump(default, include_attributes=False)
                            ),
                        )
                    )
                if args.kwarg is not None:
                    parameters.append(
                        Parameter(args.kwarg.arg, "**kwargs", False, None)
                    )
                result[prefix + node.name] = tuple(parameters)

    visit(ast.parse(source).body)
    return result


def audit_simple_signatures(
    source: str,
    simple_methods: dict,
    optional_parameters: dict,
) -> list[SignatureIssue]:
    """Check the *effective emitted callee*, including explicit adapters.

    ``IRBuilder_add_incoming`` and ``IRBuilder_as_pointer`` are real top-level
    adapters. They must not be mistaken for missing IRBuilder class methods.
    alloca's bespoke lowerer forwards size/name although the old simple table
    also contains an alloca entry.
    """
    definitions = definition_signatures(source)
    issues = []
    for method, (_return_kind, required_count) in sorted(simple_methods.items()):
        target = "IRBuilder." + method
        if target not in definitions:
            target = "IRBuilder_" + method
        signature = definitions.get(target)
        if signature is None:
            issues.append(
                SignatureIssue(
                    method, target, "missing-provider", "no real provider definition"
                )
            )
            continue
        if not signature or signature[0].kind != "pos" or not signature[0].required:
            issues.append(
                SignatureIssue(
                    method,
                    target,
                    "receiver",
                    "callee must accept one required positional receiver",
                )
            )
            continue
        signature = signature[1:]
        optional = (
            ("size", "name")
            if method == "alloca"
            else optional_parameters.get(method, ())
        )
        if any(parameter.kind != "pos" for parameter in signature):
            issues.append(
                SignatureIssue(method, target, "parameter-kind", repr(signature))
            )
        required = tuple(parameter for parameter in signature if parameter.required)
        actual_optional = tuple(
            parameter for parameter in signature if not parameter.required
        )
        if len(required) != required_count:
            issues.append(
                SignatureIssue(
                    method,
                    target,
                    "required-count",
                    f"table={required_count}; provider={len(required)}",
                )
            )
        if tuple(parameter.name for parameter in actual_optional) != tuple(optional):
            issues.append(
                SignatureIssue(
                    method,
                    target,
                    "optional-parameters",
                    f"table={tuple(optional)!r}; provider={tuple(p.name for p in actual_optional)!r}",
                )
            )
        for parameter in actual_optional:
            # These are the omission branches actually used by the current
            # emitter. A new optional parameter is an error, even if the
            # source default looks harmless or a legacy keyword whitelist
            # would silently discard it.
            if parameter.name == "name":
                default = "Constant(value='')"
            elif parameter.name in ("size", "align", "syncscope", "typ"):
                default = "Constant(value=None)"
            else:
                continue  # The optional-parameters issue above names the gap.
            if parameter.default != default:
                issues.append(
                    SignatureIssue(
                        method,
                        target,
                        "default-value",
                        f"{parameter.name}: emitted={default}; provider={parameter.default}",
                    )
                )
    return issues


def current_signature_issues(provider: Path | None = None) -> list[SignatureIssue]:
    root = Path(__file__).resolve().parents[1]
    if provider is None:
        provider = root / "ir" / "ir.py"
    scaffold = root / "frontends/python/codegen/ir_scaffold_lowering.py"
    tables = {}
    wanted = {"_IR_SCAFFOLD_SIMPLE_METHODS", "_IR_SCAFFOLD_METHOD_OPTIONAL_PARAMS"}
    for node in ast.parse(scaffold.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name in wanted:
                tables[name] = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    tables[target.id] = ast.literal_eval(node.value)
    if tables.keys() != wanted:
        raise ValueError("scaffold signature tables are missing or no longer literal")
    return audit_simple_signatures(
        provider.read_text(encoding="utf-8"),
        tables["_IR_SCAFFOLD_SIMPLE_METHODS"],
        tables["_IR_SCAFFOLD_METHOD_OPTIONAL_PARAMS"],
    )
