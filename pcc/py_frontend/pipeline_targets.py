"""Target-triple text and file contracts for the Python frontend pipeline."""

from __future__ import annotations

import sys
import platform

from typing import Optional


class PipelineTargetError(ValueError):
    """A requested target cannot be represented safely in frontend IR."""


def platform_link_flags(platform: Optional[str] = None) -> list[str]:
    """Return deterministic executable-link flags for the selected platform."""
    selected = sys.platform if platform is None else str(platform)
    if selected.startswith("linux"):
        return ["-no-pie", "-Wl,--build-id=none", "-s"]
    return []


def host_target_triple() -> str:
    """Canonical host ABI shared by frontend and object emission, without cc."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x64"):
        machine = "x86_64"
    if sys.platform == "darwin":
        if machine == "aarch64":
            machine = "arm64"
        return machine + "-apple-darwin"
    if sys.platform.startswith("linux"):
        return machine + "-unknown-linux-gnu"
    return "unknown-unknown-unknown"


def replace_module_target_text(ir_text: str, triple: str) -> tuple[str, bool]:
    marker = 'target triple = "'
    replacement = marker + str(triple) + '"'
    index = ir_text.find(marker)
    if index == -1:
        return replacement + "\n" + ir_text, True
    close = -1
    scan = index + len(marker)
    while scan < len(ir_text):
        if ir_text[scan] == '"':
            close = scan
            break
        scan += 1
    if close == -1:
        return ir_text, False
    current = ir_text[index + len(marker):close]
    if current == triple:
        return ir_text, False
    return ir_text[:index] + replacement + ir_text[close + 1:], True


def ensure_module_target(path: str, triple: str) -> str:
    path = str(path)
    with open(path, "r", encoding="utf-8") as stream:
        ir_text = stream.read()
    rewritten, changed = replace_module_target_text(ir_text, str(triple))
    if changed:
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(rewritten)
    return path


def write_utf8_text_file(path: str, text: str) -> None:
    with open(str(path), "w", encoding="utf-8") as stream:
        stream.write(str(text))


def normalize_clang_target_triple(triple: str) -> str:
    triple = str(triple).strip()
    if not triple:
        return triple
    lower = triple.lower()
    marker = "-apple-darwin"
    marker_index = lower.find(marker)
    if marker_index == -1:
        return triple
    prefix = triple[:marker_index]
    suffix = triple[marker_index + len(marker):]
    if not suffix:
        return triple
    version_text = suffix.split(".", 1)[0]
    try:
        darwin_major = int(version_text.split("-", 1)[0])
    except ValueError:
        return triple
    macos_major = darwin_major - 9
    if darwin_major <= 0 or macos_major <= 0:
        return triple
    return prefix + "-apple-macosx" + str(macos_major) + ".0.0"


def link_input_target_triple(ll_paths: list[str]) -> Optional[str]:
    marker = 'target triple = "'
    for ll_path in ll_paths:
        try:
            with open(ll_path, encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line.startswith(marker):
                        continue
                    value = line[len(marker):]
                    if value.endswith('"'):
                        return value[:-1]
                    return value.split('"', 1)[0]
        except (OSError, TypeError):
            continue
    return None


def clang_target_triple(
    ll_paths: list[str],
    *,
    host_target_triple: str,
) -> Optional[str]:
    triple = link_input_target_triple(ll_paths)
    if triple == "unknown-unknown-unknown":
        triple = None
    if triple is None:
        triple = str(host_target_triple)
    if triple == "unknown-unknown-unknown":
        return None
    return normalize_clang_target_triple(triple)


def self_backend_ir_text(ir_text: str, *, host_target_triple: str) -> str:
    ir_text = str(ir_text)
    placeholder = 'target triple = "unknown-unknown-unknown"'
    header = ir_text[:4096]
    index = header.find(placeholder)
    directive = 'target triple = "' + str(host_target_triple) + '"'
    if index >= 0:
        return ir_text[:index] + directive + ir_text[index + len(placeholder):]
    if 'target triple = "' not in header:
        return directive + "\n" + ir_text
    return ir_text


def ir_text_with_target_triple(
    ir_text: str,
    target_triple: Optional[str],
) -> str:
    if target_triple is None:
        return ir_text
    target = str(target_triple).strip()
    if not target or '"' in target or "\n" in target or "\r" in target:
        raise PipelineTargetError(
            "invalid Python frontend target triple: " + target
        )
    directive = 'target triple = "' + target + '"'
    lines = ir_text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith('target triple = "'):
            lines[index] = directive
            suffix = "\n" if ir_text.endswith("\n") else ""
            return "\n".join(lines) + suffix
    return directive + "\n" + ir_text
