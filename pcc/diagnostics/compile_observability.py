"""Wire diagnostics/profile recording into real compiler entry points.

This module is deliberately small and side-effect free.  CLI entry points wrap
their real compile function with :func:`observed_compile`; the wrapper records a
profile JSON file when requested and converts hard errors into structured
diagnostics instead of printing plain exceptions.

Unlike the earlier roadmap catalog helpers, this is not a status matrix.  It is
meant to sit on the hot user-facing path:

    cli_bootstrap.py -> observed_compile(_compile_python, ...)

The full CLI can adopt the same wrapper around its C and Python paths.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable, Optional

from pcc.diagnostics import Diagnostic, DiagnosticBag, DiagnosticSeverity, DiagnosticSpan, diagnostic_from_exception, emit_diagnostics
from pcc.diagnostics.profile_events import ProfileRecorder, write_profile_json


_VALID_DIAGNOSTIC_FORMATS = frozenset({"text", "json", "sarif"})


def _normalize_diagnostic_format(value: Optional[str]) -> str:
    fmt = (value or "text").strip().lower()
    if fmt not in _VALID_DIAGNOSTIC_FORMATS:
        raise ValueError("--diagnostic-format must be text, json, or sarif")
    return fmt


@dataclass(frozen=True)
class ObservabilityOptions:
    diagnostic_format: str = "text"
    profile_json: Optional[str] = None
    explain_fallback: bool = False
    phase: str = "compile"
    entry: str = "pcc"

    def __post_init__(self) -> None:
        try:
            fmt = _normalize_diagnostic_format(self.diagnostic_format)
        except ValueError as exc:
            raise ValueError(
                "invalid diagnostic format "
                f"{self.diagnostic_format!r}; expected text, json, or sarif"
            ) from exc
        object.__setattr__(self, "diagnostic_format", fmt)


class ObservedCompileError(RuntimeError):
    """Raised after a compile exception has been formatted as diagnostics."""

    def __init__(self, formatted: str, bag: DiagnosticBag) -> None:
        super().__init__(formatted)
        self.formatted = formatted
        self.bag = bag


def parse_observability_cli_option(
    arg: str,
    argv: list[str],
    index: int,
) -> tuple[str, Optional[str], int] | None:
    """Parse one observability option.

    Returns ``(name, value, next_index)`` or ``None`` when ``arg`` is unrelated.
    The parser is shared by bootstrap and full CLI code to keep flag semantics
    identical without forcing argparse into the self-host path.
    """
    if arg.startswith("--diagnostic-format="):
        return (
            "diagnostic_format",
            _normalize_diagnostic_format(arg.split("=", 1)[1]),
            index + 1,
        )
    if arg == "--diagnostic-format":
        if index + 1 >= len(argv):
            raise ValueError("--diagnostic-format requires a value")
        return (
            "diagnostic_format",
            _normalize_diagnostic_format(argv[index + 1]),
            index + 2,
        )
    if arg.startswith("--profile-json="):
        return ("profile_json", arg.split("=", 1)[1], index + 1)
    if arg == "--profile-json":
        if index + 1 >= len(argv):
            raise ValueError("--profile-json requires a value")
        return ("profile_json", argv[index + 1], index + 2)
    if arg == "--explain-fallback":
        return ("explain_fallback", "1", index + 1)
    return None


def format_observability_help() -> str:
    return (
        "  --diagnostic-format FMT   text (default), json, or sarif for hard errors.\n"
        "  --profile-json PATH       Write compiler phase/profile JSON.\n"
        "  --explain-fallback        Include native/fallback routing details when known.\n"
    )


def observed_compile(
    compile_fn: Callable[..., Any],
    *args: Any,
    options: ObservabilityOptions,
    metadata: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> Any:
    """Run ``compile_fn`` with diagnostics/profile observability.

    ``compile_fn`` is still the real compiler implementation.  The wrapper
    records wall-time and selected options, then re-raises failures as
    :class:`ObservedCompileError` carrying the formatted diagnostics text.
    """
    recorder = ProfileRecorder()
    recorder.set_metadata("entry", options.entry)
    recorder.set_metadata("phase", options.phase)
    if metadata:
        for key, value in metadata.items():
            recorder.set_metadata(key, value)

    try:
        with recorder.phase(options.phase, metadata=_compile_metadata(args, kwargs)):
            return compile_fn(*args, **kwargs)
    except ObservedCompileError:
        raise
    except Exception as exc:
        span = getattr(exc, "diagnostic_span", None)
        if not isinstance(span, DiagnosticSpan):
            span = _diagnostic_span_from_compile_args(args)
        diagnostic = _diagnostic_for_compile_exception(
            exc,
            options=options,
            metadata=metadata or {},
            span=span,
        )
        bag = DiagnosticBag([diagnostic])
        formatted = emit_diagnostics(bag, fmt=options.diagnostic_format)
        raise ObservedCompileError(formatted, bag) from exc
    finally:
        if options.profile_json:
            parent = os.path.dirname(os.path.abspath(options.profile_json))
            if parent:
                os.makedirs(parent, exist_ok=True)
            write_profile_json(options.profile_json, recorder)


def _compile_metadata(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if args:
        meta["input"] = str(args[0])
    for key in (
        "emit_llvm_only",
        "libpython_mode",
        "ir_scaffold_mode",
        "backend",
        "python_library",
        "recursive_stdlib",
    ):
        if key in kwargs:
            meta[key] = kwargs[key]
    return meta


def _diagnostic_span_from_compile_args(args: tuple[Any, ...]) -> Optional[DiagnosticSpan]:
    if not args:
        return None
    first = args[0]
    if isinstance(first, (str, os.PathLike)):
        return DiagnosticSpan(file=os.fspath(first))
    return None


def _diagnostic_for_compile_exception(
    exc: Exception,
    *,
    options: ObservabilityOptions,
    metadata: dict[str, Any],
    span: Optional[DiagnosticSpan] = None,
) -> Diagnostic:
    diag = diagnostic_from_exception(
        exc,
        code="PCC-PY-COMPILE-001",
        phase=options.phase,
        span=span,
        docs="pcc_multi_year_roadmap.md#92-compiler-diagnostics",
    )
    notes = list(diag.notes)
    original_exception_type = getattr(exc, "original_exception_type", "")
    if original_exception_type:
        notes[0] = "exception_type=" + str(original_exception_type)
    if metadata:
        notes.append("metadata=" + repr(metadata))
    if options.explain_fallback:
        notes.append(
            "fallback_explain=libpython fallback is controlled by "
            "--python-libpython/PCC_PYTHON_LIBPYTHON"
        )
    return Diagnostic(
        code=diag.code,
        message=diag.message,
        severity=DiagnosticSeverity.ERROR,
        phase=diag.phase,
        span=diag.span,
        notes=tuple(notes),
        suggested_fix="rerun with --diagnostic-format=json for machine-readable output",
        docs=diag.docs,
        metadata=diag.metadata,
    )
