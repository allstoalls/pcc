"""Shared diagnostic/profile CLI helpers for pcc."""
from __future__ import annotations

from contextlib import contextmanager
import sys
from typing import Iterator, Optional

from pcc.diagnostics import diagnostic_from_exception, emit_diagnostics
from pcc.diagnostics.profile_events import ProfileRecorder, write_profile_json


DIAGNOSTIC_FORMATS = ("text", "json", "sarif")


def normalize_diagnostic_format(value: Optional[str]) -> str:
    fmt = (value or "text").strip().lower()
    if fmt not in DIAGNOSTIC_FORMATS:
        raise ValueError(
            "invalid --diagnostic-format "
            f"{value!r}; expected text, json, or sarif"
        )
    return fmt


def emit_exception_diagnostic(
    exc: BaseException,
    *,
    fmt: str = "text",
    phase: str = "cli",
    code: str = "PCC-CLI-001",
) -> str:
    return emit_diagnostics(
        [diagnostic_from_exception(exc, code=code, phase=phase)],
        fmt=normalize_diagnostic_format(fmt),
    )


def write_exception_diagnostic(
    exc: BaseException,
    *,
    fmt: str = "text",
    phase: str = "cli",
    code: str = "PCC-CLI-001",
    stream=None,
) -> None:
    if stream is None:
        stream = sys.stderr
    stream.write(emit_exception_diagnostic(exc, fmt=fmt, phase=phase, code=code))
    stream.write("\n")


@contextmanager
def profile_scope(
    path: Optional[str],
    *,
    command: str,
    metadata: Optional[dict[str, object]] = None,
) -> Iterator[ProfileRecorder]:
    recorder = ProfileRecorder()
    recorder.set_metadata("command", command)
    if metadata:
        for key, value in metadata.items():
            recorder.set_metadata(key, value)
    with recorder.phase(command):
        yield recorder
    if path:
        write_profile_json(path, recorder)


def parse_observability_args(argv: list[str]) -> tuple[list[str], str, Optional[str]]:
    rest: list[str] = []
    fmt = "text"
    profile_json: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--diagnostic-format="):
            fmt = normalize_diagnostic_format(arg.split("=", 1)[1])
            i += 1
            continue
        if arg == "--diagnostic-format":
            if i + 1 >= len(argv):
                raise ValueError("--diagnostic-format requires a value")
            fmt = normalize_diagnostic_format(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--profile-json="):
            profile_json = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--profile-json":
            if i + 1 >= len(argv):
                raise ValueError("--profile-json requires a value")
            profile_json = argv[i + 1]
            i += 2
            continue
        rest.append(arg)
        i += 1
    return rest, fmt, profile_json
