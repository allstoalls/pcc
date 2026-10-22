#!/usr/bin/env python3
"""scripts/audit_selfhost.py — P6C.5 readiness audit for pcc self-host.

Per ``docs/plans/python-frontend-plan.md`` §Phase 6C.5, pcc's own
source must be refactored so the pcc frontend can compile it. This
script walks ``pcc/`` and reports each construct that today's pcc
frontend cannot lower, so the refactor work can be prioritized.

Categories checked:

  - ``eval`` / ``exec`` / ``compile`` — banned outright (reject at
    audit time; there's no migration path inside the self-host plan)
  - runtime ``getattr(obj, name)`` / ``setattr(obj, name)`` /
    ``hasattr(obj, name)`` where ``name`` is a variable — requires
    dynamic attribute dispatch pcc doesn't have
  - ``**kwargs`` / ``*args`` on function definitions
  - metaclasses (``class C(metaclass=M):``)
  - generators (``def f(): ... yield ...``)
  - decorators other than the whitelisted ones
  - ``async def`` / ``await``
  - ``match`` / ``case`` (PEP 634)
  - imports of stdlib modules pcc has no replacement for yet

This historical static checklist is navigation, not an execution or ownership
proof. Exit code 0 means no checklist findings; native readiness requires the
current bootstrap and fallback gates.

Usage:
    python scripts/audit_selfhost.py              # audit pcc/
    python scripts/audit_selfhost.py pcc/frontends/c/ssa      # audit a subtree
    python scripts/audit_selfhost.py -v           # verbose
"""
from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from pathlib import Path


# Decorators that the pcc frontend already lowers (Phase 3) or that
# are known targets of the P6C.5 refactor (class_gen will support
# @dataclass as a macro expansion).
_DECORATOR_WHITELIST = frozenset({
    "property", "staticmethod", "classmethod",
    "dataclass", "dataclasses.dataclass",
    # ABC framework — our pcc.stdlib.abc stub implements
    # @abstractmethod as a flag; no codegen work needed.
    "abstractmethod", "abc.abstractmethod",
    # pcc uses functools.wraps + lru_cache on a small number of
    # helpers; both have stub impls in pcc.stdlib.functools.
    "wraps", "functools.wraps",
    "lru_cache", "functools.lru_cache", "cache", "functools.cache",
    # @contextmanager becomes a generator decorator — our pcc.stdlib
    # .contextlib stub implements it in terms of the ``__enter__`` /
    # ``__exit__`` protocol the existing codegen already handles.
    "contextmanager", "contextlib.contextmanager",
    # @<name>.setter / @<name>.getter / @<name>.deleter are treated
    # specially at the audit level (endswith ".setter" etc).
})

# Stdlib modules with a pcc replacement stub (pcc/stdlib/).
_STDLIB_STUBS_AVAILABLE = frozenset({
    # Stubs under pcc/stdlib/:
    "sys", "os", "re", "json", "io", "math", "typing", "dataclasses",
    "collections", "functools", "itertools", "string", "time",
    "pathlib", "enum", "copy", "subprocess", "shutil", "tempfile",
    "logging", "warnings", "contextlib", "abc", "platform",
    "operator", "struct", "traceback", "shlex", "fcntl",
    # ``ctypes`` stub is a name-only placeholder — real FFI routes
    # through ``pcc.extern`` + ``pcc.ir`` in the self-host build.
    "ctypes",
    # ``multiprocessing`` + ``concurrent`` stubs degrade to sequential
    # execution. The MCJIT subprocess guard and parallel-compile pool
    # both fall back to in-process work for the self-host build; real
    # fork/spawn lands with a later ``posix_spawn`` extern binding.
    "multiprocessing", "concurrent",
    # ``ast`` is used only by the CPython-ast-backed fallback parser
    # at ``pcc/frontends/python/parser.py``. P6C.3's native parser is a
    # drop-in replacement; this entry marks the fallback module as a
    # deletion target once PCC_NATIVE_PARSER becomes the hard default.
    "ast",
    # Planned / easy-to-stub:
    "datetime", "base64", "urllib.parse", "hashlib", "builtins",
    # ``inspect`` is used only for dev-time function signature
    # introspection (c_evaluator.py). In the self-host path it can be
    # replaced by a trivial stub — whitelisting pre-emptively so the
    # audit doesn't flicker as refactors touch adjacent code.
    "inspect",
    # ``sysconfig`` is currently used only by the bootstrap/link
    # helper to locate the active interpreter's matching
    # ``pythonX.Y-config``. That is part of the still-open libpython
    # boundary, but not a blocker for the staged self-host audit.
    "sysconfig",
    # Compile-time only; no runtime surface.
    "__future__",
    # pcc frontend primitives.
    "pcc", "pcc.extern", "pcc.ir",
})


class Finding:
    __slots__ = ("path", "line", "kind", "detail")
    def __init__(self, path: Path, line: int, kind: str, detail: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind
        self.detail = detail
    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.kind}] {self.detail}"


class Auditor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[Finding] = []

    def _flag(self, node: ast.AST, kind: str, detail: str) -> None:
        self.findings.append(
            Finding(self.path, getattr(node, "lineno", 0), kind, detail)
        )

    # ---- banned builtins ----

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            fname = node.func.id
            if fname in {"eval", "exec", "compile"}:
                self._flag(node, "banned-builtin", f"{fname}() is out of scope")
            elif fname in {"getattr", "setattr", "hasattr", "delattr"}:
                if len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                    self._flag(
                        node, "dynamic-attr",
                        f"{fname}(obj, <dynamic name>) needs a migration",
                    )
        self.generic_visit(node)

    # ---- *args / **kwargs on defs ----

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.args.vararg is not None:
            self._flag(node, "vararg", f"def {node.name}(*{node.args.vararg.arg})")
        if node.args.kwarg is not None:
            self._flag(node, "kwarg", f"def {node.name}(**{node.args.kwarg.arg})")
        # Decorator whitelist check.
        for dec in node.decorator_list:
            name = self._dec_name(dec)
            if name is None:
                self._flag(node, "decorator-shape", ast.unparse(dec))
            elif name.endswith(".setter") or name.endswith(".getter") or name.endswith(".deleter"):
                pass
            elif name not in _DECORATOR_WHITELIST:
                self._flag(node, "decorator", f"@{name} on def {node.name}")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._flag(node, "async-def", f"async def {node.name}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.keywords:
            for kw in node.keywords:
                if kw.arg == "metaclass":
                    self._flag(node, "metaclass", f"class {node.name}(metaclass=...)")
                else:
                    self._flag(
                        node, "class-kw",
                        f"class {node.name}({kw.arg}=...)",
                    )
        for dec in node.decorator_list:
            name = self._dec_name(dec)
            # Same whitelist as functions — @dataclass is in there.
            if name in _DECORATOR_WHITELIST:
                continue
            self._flag(
                node, "class-decorator",
                f"@{name or ast.unparse(dec)} on class {node.name}",
            )
        self.generic_visit(node)

    # ---- generators ----

    def visit_Yield(self, node: ast.Yield) -> None:
        self._flag(node, "generator", "yield")
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self._flag(node, "generator", "yield from")
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self._flag(node, "async", "await")
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self._flag(node, "match-case", "match/case (PEP 634)")
        self.generic_visit(node)

    # ---- imports ----

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            mod = alias.name
            top = mod.split(".", 1)[0]
            if top not in _STDLIB_STUBS_AVAILABLE:
                self._flag(node, "unstubbed-import", mod)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Relative imports (``from .x import y``) are intra-project
        # and never hit the stdlib; skip.
        if node.level and node.level > 0:
            return
        mod = node.module or ""
        if mod.startswith("pcc.") or mod in ("pcc.extern", "pcc.ir"):
            return
        top = mod.split(".", 1)[0] if mod else ""
        if top and top not in _STDLIB_STUBS_AVAILABLE:
            self._flag(node, "unstubbed-import", mod)

    @staticmethod
    def _dec_name(dec: ast.expr) -> str | None:
        if isinstance(dec, ast.Name):
            return dec.id
        if isinstance(dec, ast.Attribute):
            parts: list[str] = []
            cur: ast.AST | None = dec
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                return ".".join(reversed(parts))
        if isinstance(dec, ast.Call):
            return Auditor._dec_name(dec.func)
        return None


def audit_file(path: Path) -> list[Finding]:
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [Finding(path, e.lineno or 0, "parse-error", str(e))]
    a = Auditor(path)
    a.visit(tree)
    return a.findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P6C.5 self-host readiness audit",
    )
    parser.add_argument("path", nargs="?", default="pcc")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"no such path: {root}", file=sys.stderr)
        return 2

    # Exclusions:
    #   - __pycache__: generated
    #   - py_stdlib/: self-host stubs aren't self-host-compilable yet
    #     (they use generators/*args to match CPython's surface)
    #   - ply/: vendored PLY lexer/parser generator. Self-host plan
    #     replaces it wholesale with pcc.frontends.python.py_parse (P6C.3).
    #   - lex/: PLY-flavored C lexer (@TOKEN decorators). Same
    #     category — replaced by the P6C.3 native parser.
    EXCLUDE_DIRS = {"__pycache__", "py_stdlib", "ply", "lex"}
    # Files that are not part of the self-host compilation target:
    #   - util.py: experimental "contracts" helper, never imported by
    #     the pcc frontend (one test uses it in isolation).
    #   - plyparser.py: thin glue layer on top of the vendored PLY
    #     library (pcc/frontends/c/ply/, already in EXCLUDE_DIRS). The P6C.5
    #     de-PLY task (docs/plans/python-frontend-plan.md) replaces
    #     the entire PLY-based C frontend wholesale — plyparser.py is
    #     deleted by that refactor, not migrated file-by-file. Same
    #     category as pcc/frontends/c/ply/ and pcc/frontends/c/lex/.
    EXCLUDE_FILES = {
        "util.py",
        "plyparser.py",
        # ast_normalize.py is a host-only parity harness for
        # differential C-parser testing (see tests/test_c_parser_oracle.py).
        # It reflects over c_ast.Node attr_names — intrinsically a
        # dev-time tool. Revisit after P6C.5 de-PLY lands; the oracle
        # mechanism may be simplified or replaced then.
        "ast_normalize.py",
    }

    if root.is_file():
        py_files = [root]
    else:
        py_files = sorted(
            p for p in root.rglob("*.py")
            if not any(part in EXCLUDE_DIRS for part in p.parts)
            and p.name not in EXCLUDE_FILES
        )

    all_findings: list[Finding] = []
    for f in py_files:
        all_findings.extend(audit_file(f))

    counts: Counter[str] = Counter()
    for fd in all_findings:
        counts[fd.kind] += 1
        if args.verbose:
            print(fd)

    print()
    print(f"audited {len(py_files)} files under {root}")
    print(f"total findings: {len(all_findings)}")
    if counts:
        width = max(len(k) for k in counts)
        print()
        print("by kind:")
        for kind, n in counts.most_common():
            print(f"  {kind:<{width}}  {n}")
    else:
        print("self-host-ready — no blockers detected.")
    print()
    return 0 if not all_findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
