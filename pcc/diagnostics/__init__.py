"""Structured diagnostics for pcc.

This module is intentionally dependency-light so it can be imported from the
Python frontend, C frontend, bootstrap helpers, and future runtime tooling.
It implements the diagnostic contract described in ``pcc_multi_year_roadmap``:
stable codes, phase names, optional source spans, notes, suggested fixes,
fallback reasons, and machine-readable output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Iterable, Optional


def _json_dumps_pretty(value: Any) -> str:
    """Return ``json.dumps(value, indent=2, sort_keys=True)`` shape.

    pcc's native JSON ABI owns deterministic sorted compact output.  Keeping
    indentation here makes the diagnostics presentation layer self-hostable
    without weakening its public pretty-JSON contract or growing a second JSON
    value encoder.  The scanner only inserts whitespace outside quoted strings,
    preserving escaped quotes, backslashes, and punctuation inside values.
    """
    compact = json.dumps(value, sort_keys=True)
    out: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    i = 0
    while i < len(compact):
        ch = compact[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif ch == "{" or ch == "[":
            out.append(ch)
            depth += 1
            closing = "}" if ch == "{" else "]"
            if i + 1 < len(compact) and compact[i + 1] != closing:
                out.append("\n")
                out.append("  " * depth)
        elif ch == "}" or ch == "]":
            depth -= 1
            opening = "{" if ch == "}" else "["
            if i > 0 and compact[i - 1] != opening:
                out.append("\n")
                out.append("  " * depth)
            out.append(ch)
        elif ch == ",":
            out.append(ch)
            out.append("\n")
            out.append("  " * depth)
            while i + 1 < len(compact) and compact[i + 1] == " ":
                i += 1
        elif ch == ":":
            out.append(": ")
            while i + 1 < len(compact) and compact[i + 1] == " ":
                i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    INTERNAL = "internal"


@dataclass(frozen=True)
class DiagnosticSpan:
    file: str
    line: int = 0
    col: int = 0
    end_line: int = 0
    end_col: int = 0

    def format_compact(self) -> str:
        """Return the stable source-range spelling used by debug traces."""
        return (
            self.file
            + ":"
            + str(self.line)
            + ":"
            + str(self.col)
            + "-"
            + str(self.end_line)
            + ":"
            + str(self.end_col)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
        }


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    phase: str = "unknown"
    span: Optional[DiagnosticSpan] = None
    notes: tuple[str, ...] = ()
    suggested_fix: Optional[str] = None
    fallback_reason: Optional[str] = None
    docs: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("diagnostic code must be non-empty")
        if not self.message:
            raise ValueError("diagnostic message must be non-empty")
        if isinstance(self.severity, str):
            object.__setattr__(
                self, "severity", DiagnosticSeverity(self.severity)
            )
        if isinstance(self.notes, list):
            object.__setattr__(self, "notes", tuple(self.notes))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity.value,
            "phase": self.phase,
            "message": self.message,
            "notes": list(self.notes),
            "metadata": dict(self.metadata),
        }
        if self.span is not None:
            out["span"] = self.span.to_json()
        if self.suggested_fix is not None:
            out["suggested_fix"] = self.suggested_fix
        if self.fallback_reason is not None:
            out["fallback_reason"] = self.fallback_reason
        if self.docs is not None:
            out["docs"] = self.docs
        return out

    def format_text(self, *, include_notes: bool = True) -> str:
        location = ""
        if self.span is not None:
            location = f"{self.span.file}:{self.span.line}:{self.span.col}: "
        head = (
            f"{location}{self.severity.value}: {self.code}: "
            f"[{self.phase}] {self.message}"
        )
        lines = [head]
        if include_notes:
            for note in self.notes:
                lines.append(f"  note: {note}")
            if self.fallback_reason:
                lines.append(f"  fallback: {self.fallback_reason}")
            if self.suggested_fix:
                lines.append(f"  fix: {self.suggested_fix}")
            if self.docs:
                lines.append(f"  docs: {self.docs}")
        return "\n".join(lines)


class DiagnosticBag:
    def __init__(self, diagnostics: Iterable[Diagnostic] = ()) -> None:
        self._diagnostics: list[Diagnostic] = list(diagnostics)

    def __iter__(self):
        return iter(self._diagnostics)

    def __len__(self) -> int:
        return len(self._diagnostics)

    def append(self, diagnostic: Diagnostic) -> None:
        self._diagnostics.append(diagnostic)

    def extend(self, diagnostics: Iterable[Diagnostic]) -> None:
        for diagnostic in diagnostics:
            self.append(diagnostic)

    def has_errors(self) -> bool:
        return any(
            d.severity in (DiagnosticSeverity.ERROR, DiagnosticSeverity.INTERNAL)
            for d in self._diagnostics
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "pcc.diagnostics.v1",
            "diagnostics": [d.to_json() for d in self._diagnostics],
            "has_errors": self.has_errors(),
        }

    def format_json(self) -> str:
        return _json_dumps_pretty(self.to_json())

    def format_text(self) -> str:
        return "\n".join(d.format_text() for d in self._diagnostics)


def diagnostic_from_exception(
    exc: BaseException,
    *,
    code: str = "PCC-INTERNAL-000",
    phase: str = "unknown",
    span: Optional[DiagnosticSpan] = None,
    docs: Optional[str] = None,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        phase=phase,
        span=span,
        message=str(exc) or type(exc).__name__,
        notes=(f"exception_type={type(exc).__name__}",),
        docs=docs,
    )


def fallback_diagnostic(
    *,
    code: str,
    phase: str,
    message: str,
    fallback_reason: str,
    span: Optional[DiagnosticSpan] = None,
    suggested_fix: Optional[str] = None,
    docs: Optional[str] = None,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=DiagnosticSeverity.WARNING,
        phase=phase,
        span=span,
        message=message,
        fallback_reason=fallback_reason,
        suggested_fix=suggested_fix,
        docs=docs,
    )


def emit_diagnostics(
    diagnostics: Iterable[Diagnostic],
    *,
    fmt: str = "text",
) -> str:
    bag = DiagnosticBag(diagnostics)
    normalized = fmt.strip().lower()
    if normalized == "text":
        return bag.format_text()
    if normalized == "json":
        return bag.format_json()
    if normalized == "sarif":
        return diagnostics_to_sarif(bag)
    raise ValueError(f"unknown diagnostic format {fmt!r}")


def diagnostics_to_sarif(bag: DiagnosticBag) -> str:
    rules = {}
    results = []
    for diagnostic in bag:
        rules.setdefault(
            diagnostic.code,
            {
                "id": diagnostic.code,
                "shortDescription": {"text": diagnostic.code},
                "fullDescription": {"text": diagnostic.message},
            },
        )
        result: dict[str, Any] = {
            "ruleId": diagnostic.code,
            "level": _sarif_level(diagnostic.severity),
            "message": {"text": diagnostic.message},
            "properties": {
                "phase": diagnostic.phase,
                "notes": list(diagnostic.notes),
            },
        }
        if diagnostic.span is not None:
            result["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": diagnostic.span.file},
                        "region": {
                            "startLine": max(1, diagnostic.span.line),
                            "startColumn": max(1, diagnostic.span.col),
                            "endLine": max(
                                max(1, diagnostic.span.line),
                                diagnostic.span.end_line or diagnostic.span.line,
                            ),
                            "endColumn": max(1, diagnostic.span.end_col or diagnostic.span.col),
                        },
                    }
                }
            ]
        results.append(result)
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pcc",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }
    return _json_dumps_pretty(sarif)


def _sarif_level(severity: DiagnosticSeverity) -> str:
    if severity == DiagnosticSeverity.INFO:
        return "note"
    if severity == DiagnosticSeverity.WARNING:
        return "warning"
    return "error"
