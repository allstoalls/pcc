#!/usr/bin/env python3
"""Inventory raw C pointer flows into object-typed positions in pcc-Python sources.

The frontend types every pointer-producing ``pcc.unsafe`` intrinsic result as
``TYPE_DYN``, so a raw C pointer is statically indistinguishable from a Python
object and both runtime mirrors must probe pointer provenance on every
refcount and class check.  Before a static raw-pointer type can be enforced,
every place where a raw pointer currently flows into an object-shaped
position has to be known.  This read-only tool walks the lifted AST of each
module (no type inference, no codegen) and reports those flows.

Raw sources
    * results of ``pcc.unsafe`` intrinsics whose table type is ``TYPE_DYN``
      (``malloc``, ``ptr_add``, ``int_to_ptr``, ``load_ptr``, ``cstr`` ...)
    * results of ``pcc.extern`` declarations whose return type is ``c_ptr``
    * locals assigned from a raw source (flow-insensitive within a function)
    * calls to functions of the same module that ``return`` a raw value
      (fixpoint), and parameters annotated ``c_ptr``

Sinks (each reported with ``file:line``)
    ``arg``        raw argument to a Python function, method or builtin call
    ``return``     raw value returned by a function with no raw annotation
    ``store``      raw value stored into an attribute, subscript or global
    ``literal``    raw value inside a container literal or other object node
    ``compare``    ``==``/``!=``/``is`` with a raw operand
    ``truth``      raw value used as a condition or boolean operand
    ``mixed``      one local bound both to raw and to non-raw values

Sanctioned raw consumers (``ptr_to_int``, ``ptr_is_null``, ``ptr_eq``,
other intrinsics, extern parameters, ``int_to_ptr``) are not sinks.

Usage:
    pcc_rawptr_flow_inventory.py [--files LIST] [--json OUT] [--sites N] FILE...

``--files`` names a text file with one repository-relative path per line
(for example the module rows of a retained codegen worker manifest).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pcc.frontends.python.py_lift import parse_and_lift  # noqa: E402
from pcc.frontends.python import py_ast as pa  # noqa: E402
from pcc.frontends.python.type_infer import (  # noqa: E402
    _UNSAFE_INTRINSIC_RETURN_TYPES,
    TYPE_DYN,
    type_eq,
)

SINK_KINDS = ("arg", "return", "store", "literal", "compare", "truth", "mixed")
SAFE_RAW_CONSUMERS = frozenset(
    {"ptr_to_int", "ptr_is_null", "ptr_eq", "ptr_diff", "int_to_ptr"}
)


def pointer_intrinsic_names() -> frozenset[str]:
    """Intrinsics whose result the frontend currently types as ``TYPE_DYN``."""
    return frozenset(
        name
        for name, ty in _UNSAFE_INTRINSIC_RETURN_TYPES.items()
        if type_eq(ty, TYPE_DYN)
    )


@dataclasses.dataclass
class Site:
    kind: str
    module: str
    function: str
    line: int
    detail: str
    origin: str = "intrinsic"


@dataclasses.dataclass
class ModuleReport:
    module: str
    path: str
    raw_sources: int = 0
    sites: list = dataclasses.field(default_factory=list)
    extern_c_ptr_symbols: list = dataclasses.field(default_factory=list)
    pointer_lane: bool = False

    def counts(self) -> dict:
        out = {kind: 0 for kind in SINK_KINDS}
        for site in self.sites:
            out[site.kind] += 1
        return out


def _children(node):
    """Yield child AST nodes of a py_ast dataclass node, in field order."""
    if not dataclasses.is_dataclass(node):
        return
    for field in dataclasses.fields(node):
        if field.name in ("span", "ty", "annotation", "return_ty"):
            continue
        value = getattr(node, field.name, None)
        yield from _flatten(value)


def _flatten(value):
    if isinstance(value, (pa.Expr, pa.Stmt)) or dataclasses.is_dataclass(value):
        yield value
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _flatten(item)


def _walk(node):
    yield node
    for child in _children(node):
        yield from _walk(child)


def _ident(expr) -> str | None:
    if isinstance(expr, pa.Name):
        return expr.ident
    return None


_RAW_POINTER_MARKERS = frozenset({"c_ptr", "c_rawptr"})
_AMBIGUOUS_RETURN_MARKERS = frozenset({"c_ptr", "c_str"})
_POINTER_LANE_DIRECTIVES = ("__pcc_freestanding__", "__pcc_runtime_port__")


def _annotation_is_c_ptr(annotation) -> bool:
    """``c_ptr`` and ``c_rawptr`` annotate raw memory; ``c_obj`` is an object."""
    if annotation is None:
        return False
    if isinstance(annotation, pa.Name):
        return annotation.ident in _RAW_POINTER_MARKERS
    name = getattr(annotation, "name", None)
    return name in _RAW_POINTER_MARKERS


def _module_is_pointer_lane(module) -> bool:
    """Freestanding kernels and runtime ports keep raw pointers in the pointer
    lane; the application-mode inventory does not apply to them."""
    for stmt in module.body:
        if not isinstance(stmt, pa.Assign):
            continue
        value = stmt.value
        if not isinstance(value, pa.BoolLit) or not value.value:
            continue
        for target in stmt.targets:
            if _ident(target) in _POINTER_LANE_DIRECTIVES:
                return True
    return False


class _ModuleAnalysis:
    def __init__(self, module: pa.Module, path: str, intrinsics: frozenset[str], extern_returns: str = "raw"):
        self.module = module
        self.path = path
        self.extern_returns = extern_returns
        self.extern_symbol_by_alias: dict[str, str] = {}
        self.intrinsic_aliases: dict[str, str] = {}
        self.extern_raw_aliases: set[str] = set()
        self.extern_aliases: set[str] = set()
        self.extern_factory = "extern"
        self.raw_functions: set[str] = set()
        self.report = ModuleReport(module=module.name, path=path)
        self._intrinsics = intrinsics
        self._collect_imports_and_externs()

    # -- module level -----------------------------------------------------
    def _collect_imports_and_externs(self) -> None:
        for stmt in self.module.body:
            if isinstance(stmt, pa.ImportFrom) and stmt.module == "pcc.unsafe":
                for name, as_name in stmt.names:
                    self.intrinsic_aliases[as_name or name] = name
            if isinstance(stmt, pa.ImportFrom) and stmt.module == "pcc.extern":
                for name, as_name in stmt.names:
                    if name == "extern":
                        self.extern_factory = as_name or name
        for stmt in self.module.body:
            if isinstance(stmt, pa.Assign) and len(stmt.targets) == 1:
                target = _ident(stmt.targets[0])
                call = stmt.value
                if (
                    target
                    and isinstance(call, pa.Call)
                    and _ident(call.func) == self.extern_factory
                ):
                    self.extern_aliases.add(target)
                    restype = _ident(call.args[2]) if len(call.args) >= 3 else None
                    if restype in _AMBIGUOUS_RETURN_MARKERS or restype == "c_rawptr":
                        if isinstance(call.args[0], pa.StrLit):
                            self.extern_symbol_by_alias[target] = call.args[0].value
                        # ``c_rawptr`` is raw by declaration; the legacy
                        # ambiguous markers follow the --extern-c-ptr-returns
                        # policy.  ``c_obj`` results are objects, never raw.
                        if restype == "c_rawptr" or self.extern_returns == "raw":
                            self.extern_raw_aliases.add(target)

    def extern_c_ptr_symbols(self) -> list[str]:
        """Extern symbols still declared with an ambiguous ``c_ptr``/``c_str``
        return in this module (application modules must use c_obj/c_rawptr)."""
        symbols = []
        for stmt in self.module.body:
            if isinstance(stmt, pa.Assign) and len(stmt.targets) == 1:
                call = stmt.value
                if (
                    isinstance(call, pa.Call)
                    and _ident(call.func) == self.extern_factory
                    and len(call.args) >= 3
                    and _ident(call.args[2]) in _AMBIGUOUS_RETURN_MARKERS
                    and isinstance(call.args[0], pa.StrLit)
                ):
                    symbols.append(call.args[0].value)
        return symbols

    def _is_pointer_intrinsic(self, name: str | None) -> bool:
        return name is not None and self.intrinsic_aliases.get(name) in self._intrinsics

    def _is_intrinsic(self, name: str | None) -> bool:
        return name is not None and name in self.intrinsic_aliases

    # -- function level ---------------------------------------------------
    def analyze(self) -> ModuleReport:
        functions = list(self._functions(self.module.body, ""))
        # Fixpoint over raw-returning module functions.
        changed = True
        while changed:
            changed = False
            for qualname, fd in functions:
                if fd.name in self.raw_functions:
                    continue
                if self._returns_raw(fd):
                    self.raw_functions.add(fd.name)
                    changed = True
        for qualname, fd in functions:
            self._analyze_function(qualname, fd)
        self._analyze_module_level()
        self.report.extern_c_ptr_symbols = self.extern_c_ptr_symbols()
        self.report.pointer_lane = _module_is_pointer_lane(self.module)
        return self.report

    def _functions(self, body, prefix):
        for stmt in body:
            if isinstance(stmt, pa.FuncDef):
                yield (prefix + stmt.name, stmt)
                yield from self._functions(stmt.body, prefix + stmt.name + ".")
            elif isinstance(stmt, pa.ClassDef):
                yield from self._functions(stmt.body, prefix + stmt.name + ".")

    def _raw_locals(self, fd: pa.FuncDef) -> tuple[dict, set[str]]:
        raw: dict = {}
        nonraw: set[str] = set()
        for arg in fd.args:
            if _annotation_is_c_ptr(arg.annotation):
                raw[arg.name] = "param:c_ptr"
        changed = True
        while changed:
            changed = False
            for stmt in self._own_statements(fd.body):
                if isinstance(stmt, pa.Assign):
                    origin = self._expr_origin(stmt.value, raw)
                    for target in stmt.targets:
                        name = _ident(target)
                        if name is None:
                            continue
                        if origin is not None:
                            if name not in raw:
                                raw[name] = origin
                                changed = True
                        elif name not in nonraw:
                            nonraw.add(name)
                            changed = True
                elif isinstance(stmt, pa.AugAssign):
                    name = _ident(stmt.target)
                    if name is not None and name not in nonraw:
                        nonraw.add(name)
                        changed = True
                elif isinstance(stmt, pa.For):
                    name = _ident(stmt.target)
                    if name is not None and name not in nonraw:
                        nonraw.add(name)
                        changed = True
        return raw, nonraw

    def _own_statements(self, body):
        """Statements of one function body, descending into control flow but
        not into nested function or class definitions."""
        for stmt in body:
            if isinstance(stmt, (pa.FuncDef, pa.ClassDef)):
                continue
            yield stmt
            for child in _children(stmt):
                if isinstance(child, pa.Stmt) and not isinstance(
                    child, (pa.FuncDef, pa.ClassDef)
                ):
                    yield from self._own_statements((child,))

    def _expr_origin(self, expr, raw_locals) -> str | None:
        if isinstance(expr, pa.Name):
            if isinstance(raw_locals, dict):
                return raw_locals.get(expr.ident)
            return "local" if expr.ident in raw_locals else None
        if isinstance(expr, pa.Call):
            callee = _ident(expr.func)
            if self._is_pointer_intrinsic(callee):
                return "intrinsic:" + self.intrinsic_aliases[callee]
            if callee in self.extern_raw_aliases:
                return "extern:" + self.extern_symbol_by_alias.get(callee, callee)
            if callee in self.raw_functions:
                return "function:" + callee
            return None
        if isinstance(expr, pa.IfExpr):
            return self._expr_origin(expr.then_e, raw_locals) or self._expr_origin(
                expr.else_e, raw_locals
            )
        return None

    def _expr_is_raw(self, expr, raw_locals) -> bool:
        return self._expr_origin(expr, raw_locals) is not None

    def _returns_raw(self, fd: pa.FuncDef) -> bool:
        if _annotation_is_c_ptr(fd.return_ty):
            return True
        raw, _ = self._raw_locals(fd)
        for stmt in self._own_statements(fd.body):
            if isinstance(stmt, pa.Return) and stmt.value is not None:
                if self._expr_is_raw(stmt.value, raw):
                    return True
        return False

    def _record(self, kind, function, node, detail, origin="intrinsic") -> None:
        line = getattr(getattr(node, "span", None), "line", 0) or 0
        self.report.sites.append(
            Site(kind=kind, module=self.module.name, function=function, line=line, detail=detail, origin=origin or "intrinsic")
        )

    def _analyze_function(self, qualname: str, fd: pa.FuncDef) -> None:
        raw, nonraw = self._raw_locals(fd)
        for name in sorted(set(raw) & nonraw):
            self._record("mixed", qualname, fd, name, raw[name])
        for stmt in self._own_statements(fd.body):
            self._analyze_statement(qualname, stmt, raw)

    def _analyze_module_level(self) -> None:
        raw: dict = {}
        for stmt in self.module.body:
            origin = self._expr_origin(stmt.value, raw) if isinstance(stmt, pa.Assign) else None
            if origin is not None:
                for target in stmt.targets:
                    name = _ident(target)
                    if name is not None:
                        raw[name] = origin
                        self._record("store", "<module>", stmt, "module global " + name, origin)
        for stmt in self.module.body:
            if not isinstance(stmt, (pa.FuncDef, pa.ClassDef)):
                self._analyze_statement("<module>", stmt, raw)

    def _analyze_statement(self, function: str, stmt, raw: set[str]) -> None:
        if isinstance(stmt, pa.Return) and stmt.value is not None:
            if self._expr_is_raw(stmt.value, raw):
                self.report.raw_sources += 1
                self._record("return", function, stmt, "raw return", self._expr_origin(stmt.value, raw))
        if isinstance(stmt, pa.Assign):
            if self._expr_is_raw(stmt.value, raw):
                self.report.raw_sources += 1
                for target in stmt.targets:
                    if isinstance(target, (pa.Attr, pa.Subscript)):
                        self._record("store", function, stmt, type(target).__name__.lower(), self._expr_origin(stmt.value, raw))
        if isinstance(stmt, pa.Global):
            for name in stmt.names:
                if name in raw:
                    self._record("store", function, stmt, "global " + name)
        for expr in self._statement_exprs(stmt):
            self._analyze_expr(function, expr, raw, condition=False)
        for cond in self._statement_conditions(stmt):
            if self._expr_is_raw(cond, raw):
                self._record("truth", function, cond, "condition", self._expr_origin(cond, raw))

    def _statement_exprs(self, stmt):
        for child in _children(stmt):
            if isinstance(child, pa.Expr):
                yield child

    def _statement_conditions(self, stmt):
        for field in ("cond", "test"):
            cond = getattr(stmt, field, None)
            if isinstance(cond, pa.Expr):
                yield cond

    def _analyze_expr(self, function: str, expr, raw: set[str], *, condition: bool) -> None:
        for node in _walk(expr):
            if isinstance(node, pa.Call):
                callee = _ident(node.func)
                if self._is_intrinsic(callee) or callee in self.extern_aliases:
                    continue
                if callee in SAFE_RAW_CONSUMERS:
                    continue
                for position, arg in enumerate(node.args):
                    if self._expr_is_raw(arg, raw):
                        target = callee or (
                            "." + node.func.name if isinstance(node.func, pa.Attr) else "<call>"
                        )
                        self._record("arg", function, node, f"{target} arg{position}", self._expr_origin(arg, raw))
                for name, arg in node.kwargs:
                    if self._expr_is_raw(arg, raw):
                        self._record("arg", function, node, f"{callee or '<call>'} kw {name}", self._expr_origin(arg, raw))
            elif isinstance(node, pa.Compare):
                if node.op in ("==", "!=", "is", "is not") and (
                    self._expr_is_raw(node.lhs, raw) or self._expr_is_raw(node.rhs, raw)
                ):
                    self._record("compare", function, node, node.op, self._expr_origin(node.lhs, raw) or self._expr_origin(node.rhs, raw))
            elif isinstance(node, pa.BoolExpr):
                for operand in (node.left, node.right):
                    if self._expr_is_raw(operand, raw):
                        self._record("truth", function, node, node.op, self._expr_origin(operand, raw))
            elif isinstance(node, pa.UnaryOp) and node.op == "not":
                if self._expr_is_raw(node.operand, raw):
                    self._record("truth", function, node, "not", self._expr_origin(node.operand, raw))
            elif isinstance(node, pa.IfExpr):
                if self._expr_is_raw(node.cond, raw):
                    self._record("truth", function, node, "ifexpr", self._expr_origin(node.cond, raw))
            elif isinstance(node, pa.Expr) and not isinstance(
                node,
                (pa.Name, pa.Attr, pa.Subscript, pa.BinOp, pa.Call, pa.Compare, pa.BoolExpr, pa.UnaryOp, pa.IfExpr, pa.Slice),
            ):
                for child in _children(node):
                    if isinstance(child, pa.Expr) and self._expr_is_raw(child, raw):
                        self._record("literal", function, node, type(node).__name__, self._expr_origin(child, raw))


def analyze_source(src: str, filename: str, module_name: str, extern_returns: str = "raw") -> ModuleReport:
    module = parse_and_lift(src, filename, module_name)
    return _ModuleAnalysis(module, filename, pointer_intrinsic_names(), extern_returns).analyze()


def analyze_file(path: Path, module_name: str | None = None, extern_returns: str = "raw") -> ModuleReport:
    name = module_name or path.stem
    return analyze_source(path.read_text(encoding="utf-8"), str(path), name, extern_returns)


def _module_name_for(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(REPO_ROOT)
        return ".".join(relative.with_suffix("").parts)
    except ValueError:
        return path.stem


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--files", help="text file with one path per line")
    parser.add_argument("--json", help="write the JSON receipt here")
    parser.add_argument("--sites", type=int, default=12, help="sites to print per kind")
    parser.add_argument(
        "--extern-c-ptr-returns",
        choices=("raw", "object"),
        default="raw",
        help="treat c_ptr extern returns as raw memory (fail-closed default) or as objects",
    )
    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.paths]
    if args.files:
        for line in Path(args.files).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                paths.append(Path(line))
    if not paths:
        parser.error("no input files")
    reports: list[ModuleReport] = []
    failures: list[str] = []
    for path in paths:
        try:
            reports.append(analyze_file(path, _module_name_for(path), args.extern_c_ptr_returns))
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
    totals = {kind: 0 for kind in SINK_KINDS}
    per_module = []
    all_sites = []
    pointer_lane_modules = [report.module for report in reports if report.pointer_lane]
    # Pointer-lane modules (freestanding kernels, runtime ports) are outside
    # the application-mode contract: they are listed, not counted.
    reports = [report for report in reports if not report.pointer_lane]
    for report in reports:
        counts = report.counts()
        for kind in SINK_KINDS:
            totals[kind] += counts[kind]
        per_module.append(
            {
                "module": report.module,
                "path": report.path,
                "raw_sources": report.raw_sources,
                "extern_c_ptr_symbols": report.extern_c_ptr_symbols,
                **counts,
            }
        )
        all_sites.extend(dataclasses.asdict(site) for site in report.sites)
    per_module.sort(key=lambda row: -sum(row[k] for k in SINK_KINDS))
    print(
        f"modules analyzed: {len(reports)}  pointer-lane modules excluded: {len(pointer_lane_modules)}"
        f"  parse failures: {len(failures)}  extern c_ptr returns treated as: {args.extern_c_ptr_returns}"
    )
    print("sink totals:", json.dumps(totals))
    origins: dict[str, int] = {}
    for site in all_sites:
        origin = site["origin"].split(":")[0]
        origins[origin] = origins.get(origin, 0) + 1
    print("sink origins:", json.dumps(origins, sort_keys=True))
    print("modules with sinks (top 20):")
    for row in per_module[:20]:
        total = sum(row[k] for k in SINK_KINDS)
        if total == 0:
            break
        print(f"  {total:4d}  {row['module']}  " + " ".join(f"{k}={row[k]}" for k in SINK_KINDS if row[k]))
    for kind in SINK_KINDS:
        sites = [s for s in all_sites if s["kind"] == kind]
        if not sites:
            continue
        print(f"{kind} sites ({len(sites)}), first {min(len(sites), args.sites)}:")
        for site in sites[: args.sites]:
            print(f"  {site['module']}:{site['line']} {site['function']} {site['detail']} [{site['origin']}]")
    extern_symbols: dict[str, int] = {}
    for row in per_module:
        for symbol in row["extern_c_ptr_symbols"]:
            extern_symbols[symbol] = extern_symbols.get(symbol, 0) + 1
    if extern_symbols:
        print(f"c_ptr-returning extern declarations ({sum(extern_symbols.values())}):")
        for symbol, count in sorted(extern_symbols.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count:3d}  {symbol}")
    for failure in failures:
        print("parse failure:", failure)
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "schema": "pcc.rawptr-flow-inventory.v3",
                    "pointer_lane_modules": pointer_lane_modules,
                    "extern_c_ptr_returns": args.extern_c_ptr_returns,
                    "pointer_intrinsics": sorted(pointer_intrinsic_names()),
                    "totals": totals,
                    "modules": per_module,
                    "sites": all_sites,
                    "parse_failures": failures,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
