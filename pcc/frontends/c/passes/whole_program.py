"""Phase 6 whole-program / cross-TU analysis.

Takes a list of translation-unit ASTs and derives:
  - function definitions by name (with their linkage),
  - call sites by callee name (with constant-arg signatures),
  - per-function "always-constant" argument classification,
  - dead internal (static/unused) functions.

This is analysis-only for now. Consumers can use the classification to
drive specialization, cross-TU dead-function removal, and range/constant
propagation across TU boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pcc.frontends.c.ast import c_ast


_KNOWN_EXPORTED = frozenset({"main"})


@dataclass(slots=True)
class FunctionInfo:
    name: str
    defined_in: str
    linkage: str  # "static", "extern", "default"
    param_names: list[str] = field(default_factory=list)
    param_count: int = 0
    is_variadic: bool = False


@dataclass(slots=True)
class CallSite:
    callee: str
    in_function: str
    in_unit: str
    # For each argument, the constant value if it's a literal int/string,
    # else None (unknown / non-constant).
    const_args: list[int | str | None] = field(default_factory=list)


@dataclass(slots=True)
class WholeProgramResult:
    functions: dict[str, FunctionInfo] = field(default_factory=dict)
    call_sites: list[CallSite] = field(default_factory=list)
    # Per-function: list of sets of constant values seen per argument position.
    # If a set is {} (empty), all call sites passed non-constant at that position.
    # If a set is {7}, all call sites passed 7 at that position.
    # If a set is {7, 8}, two distinct constants seen → not specializable to one.
    const_args: dict[str, list[set]] = field(default_factory=dict)
    # Functions with internal linkage that have no callers (dead code).
    dead_internal_functions: set[str] = field(default_factory=set)
    # Functions with internal linkage whose every call site passes the same
    # constant at some argument position (specialization candidates).
    specialization_candidates: dict[str, dict[int, int | str]] = field(default_factory=dict)


class WholeProgramAnalyzer:
    """Scan multiple TUs and derive cross-TU facts."""

    def analyze(
        self,
        asts: list[tuple[str, c_ast.FileAST]],
    ) -> WholeProgramResult:
        """Analyze a list of (unit_name, file_ast) pairs."""
        result = WholeProgramResult()

        # Pass 1: record every function definition.
        for unit_name, ast in asts:
            for ext in ast.ext or ():
                if isinstance(ext, c_ast.FuncDef):
                    info = self._collect_function_info(ext, unit_name)
                    if info.name:
                        result.functions[info.name] = info

        # Pass 2: record every call site with its constant-arg signature.
        for unit_name, ast in asts:
            for ext in ast.ext or ():
                if isinstance(ext, c_ast.FuncDef):
                    caller_name = getattr(ext.decl, "name", "") or ""
                    self._collect_calls_in_function(
                        ext, caller_name, unit_name, result,
                    )

        # Pass 3: per-function arg-constant classification.
        for func_name, info in result.functions.items():
            arg_sets: list[set] = [set() for _ in range(info.param_count)]
            for call in result.call_sites:
                if call.callee != func_name:
                    continue
                for i in range(min(info.param_count, len(call.const_args))):
                    c = call.const_args[i]
                    if c is not None:
                        arg_sets[i].add(c)
            result.const_args[func_name] = arg_sets

        # Pass 4: dead-internal-function detection.
        callers_of: dict[str, set[str]] = {}
        for call in result.call_sites:
            callers_of.setdefault(call.callee, set()).add(call.in_function)
        for func_name, info in result.functions.items():
            if info.linkage != "static":
                continue
            if func_name in _KNOWN_EXPORTED:
                continue
            if not callers_of.get(func_name):
                result.dead_internal_functions.add(func_name)

        # Pass 5: specialization candidates (internal-linkage only).
        for func_name, info in result.functions.items():
            if info.linkage != "static":
                continue
            if func_name in result.dead_internal_functions:
                continue
            arg_sets = result.const_args.get(func_name, [])
            call_count = sum(
                1 for c in result.call_sites if c.callee == func_name
            )
            if call_count == 0:
                continue
            candidates: dict[int, int | str] = {}
            for i, vals in enumerate(arg_sets):
                # All observed constants are the same → can specialize.
                # But we only have confidence if EVERY call site had a constant
                # at this position (not None).
                every_call_constant = all(
                    c.callee != func_name
                    or (i < len(c.const_args) and c.const_args[i] is not None)
                    for c in result.call_sites
                )
                if every_call_constant and len(vals) == 1:
                    candidates[i] = next(iter(vals))
            if candidates:
                result.specialization_candidates[func_name] = candidates

        return result

    def _collect_function_info(
        self, funcdef: c_ast.FuncDef, unit_name: str,
    ) -> FunctionInfo:
        name = getattr(funcdef.decl, "name", "") or ""
        storage = set(getattr(funcdef.decl, "storage", None) or ())
        if "static" in storage:
            linkage = "static"
        elif "extern" in storage:
            linkage = "extern"
        else:
            linkage = "default"
        # Get parameter list.
        param_names: list[str] = []
        is_variadic = False
        func_decl = funcdef.decl.type
        if isinstance(func_decl, c_ast.FuncDecl):
            params = (
                getattr(func_decl.args, "params", None) or [] if func_decl.args else []
            )
            for p in params:
                if isinstance(p, c_ast.EllipsisParam):
                    is_variadic = True
                    continue
                pname = getattr(p, "name", None)
                param_names.append(pname or "")
        return FunctionInfo(
            name=name,
            defined_in=unit_name,
            linkage=linkage,
            param_names=param_names,
            param_count=len(param_names),
            is_variadic=is_variadic,
        )

    def _collect_calls_in_function(
        self,
        funcdef: c_ast.FuncDef,
        caller_name: str,
        unit_name: str,
        result: WholeProgramResult,
    ) -> None:
        for node in _walk(funcdef):
            if not isinstance(node, c_ast.FuncCall):
                continue
            if not isinstance(node.name, c_ast.ID):
                continue
            callee = node.name.name
            args = getattr(node.args, "exprs", None) or ()
            const_args: list[int | str | None] = []
            for arg in args:
                const_args.append(self._extract_constant(arg))
            result.call_sites.append(
                CallSite(
                    callee=callee,
                    in_function=caller_name,
                    in_unit=unit_name,
                    const_args=const_args,
                )
            )

    def _extract_constant(
        self, node: c_ast.Node,
    ) -> int | str | None:
        if isinstance(node, c_ast.Constant):
            if node.type == "int":
                try:
                    raw = node.value.strip()
                    # Strip suffixes like u/l/ul/ull.
                    while raw and raw[-1].lower() in "ul":
                        raw = raw[:-1]
                    return int(raw, 0)
                except Exception:
                    return None
            if node.type == "string":
                return node.value
            return None
        if isinstance(node, c_ast.UnaryOp) and node.op == "-":
            inner = self._extract_constant(node.expr)
            if isinstance(inner, int):
                return -inner
        return None


def _walk(node):
    """Return all descendant nodes of a c_ast node (pre-order)."""
    out: list = []
    _walk_into(node, out)
    return out


def _walk_into(node, out: list) -> None:
    if node is None:
        return
    out.append(node)
    for _, child in node.children():
        _walk_into(child, out)
