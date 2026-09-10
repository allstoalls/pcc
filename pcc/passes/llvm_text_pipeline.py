"""Helpers for explicit LLVM textual pass pipelines.

This module keeps LLVM's default O1/O2/O3 pipelines out of the "black box"
path by:

1. expanding pipeline specs such as ``default<O2>`` via ``opt
   --print-pipeline-passes``,
2. parsing the resulting textual pipeline into a tree,
3. pruning disabled leaf passes, and
4. running the filtered pipeline through ``opt -passes=...``.

The execution still happens inside LLVM. We only make the pipeline explicit and
manageable from Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
import re
import shutil
import subprocess

from .llvm_builtin_registry import (
    LLVM_DEFAULT_PROFILE_PASSES,
    LLVM_DEFAULT_PROFILE_VERSION,
)

_DEFAULT_OPT_CANDIDATES = (
    "/opt/homebrew/opt/llvm@20/bin/opt",
    "/usr/local/opt/llvm@20/bin/opt",
    "opt",
)
_MINIMAL_MODULE = "define i32 @main(){ ret i32 0 }\n"
_LLVM_VERSION_RE = re.compile(r"version\s+([0-9]+(?:\.[0-9]+){0,2})")


@dataclass(frozen=True)
class LLVMPipelineNode:
    """One node in LLVM's textual pass pipeline."""

    name: str
    params: str = ""
    children: tuple["LLVMPipelineNode", ...] = ()

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def label(self) -> str:
        if self.params:
            return f"{self.name}<{self.params}>"
        return self.name


def _split_top_level(text: str) -> list[str]:
    parts = []
    start = 0
    paren_depth = 0
    angle_depth = 0

    for i, ch in enumerate(text):
        if ch == "," and paren_depth == 0 and angle_depth == 0:
            piece = text[start:i].strip()
            if piece:
                parts.append(piece)
            start = i + 1
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == "<":
            angle_depth += 1
        elif ch == ">":
            angle_depth -= 1

    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _find_top_level_char(text: str, target: str) -> int:
    paren_depth = 0
    angle_depth = 0
    for i, ch in enumerate(text):
        if ch == target and paren_depth == 0 and angle_depth == 0:
            return i
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == "<":
            angle_depth += 1
        elif ch == ">":
            angle_depth -= 1
    return -1


def _split_label_params(text: str) -> tuple[str, str]:
    angle_pos = _find_top_level_char(text, "<")
    if angle_pos < 0:
        return text, ""
    return text[:angle_pos], text[angle_pos + 1 : -1]


def _parse_node(text: str) -> LLVMPipelineNode:
    text = text.strip()
    paren_pos = _find_top_level_char(text, "(")
    if paren_pos < 0:
        name, params = _split_label_params(text)
        return LLVMPipelineNode(name=name, params=params)

    if not text.endswith(")"):
        raise ValueError(f"invalid LLVM pipeline node: {text!r}")
    head = text[:paren_pos]
    inner = text[paren_pos + 1 : -1]
    name, params = _split_label_params(head)
    return LLVMPipelineNode(
        name=name,
        params=params,
        children=tuple(_parse_node(piece) for piece in _split_top_level(inner)),
    )


def parse_pipeline(text: str) -> tuple[LLVMPipelineNode, ...]:
    """Parse LLVM's textual pipeline syntax into a tree."""
    text = str(text or "").strip()
    if not text:
        return ()
    return tuple(_parse_node(piece) for piece in _split_top_level(text))


def serialize_pipeline(nodes: tuple[LLVMPipelineNode, ...] | list[LLVMPipelineNode]) -> str:
    """Serialize a parsed pipeline tree back to LLVM's textual syntax."""

    def _render(node: LLVMPipelineNode) -> str:
        head = node.label
        if node.children:
            return f"{head}({','.join(_render(child) for child in node.children)})"
        return head

    return ",".join(_render(node) for node in nodes)


def leaf_pass_names(nodes: tuple[LLVMPipelineNode, ...] | list[LLVMPipelineNode]) -> tuple[str, ...]:
    """Return leaf pass names in execution order, keeping duplicates."""

    leaves: list[str] = []

    def _walk(node: LLVMPipelineNode):
        if node.is_leaf:
            leaves.append(node.name)
            return
        for child in node.children:
            _walk(child)

    for node in nodes:
        _walk(node)
    return tuple(leaves)


def prune_disabled_passes(
    nodes: tuple[LLVMPipelineNode, ...] | list[LLVMPipelineNode],
    disabled_passes,
) -> tuple[LLVMPipelineNode, ...]:
    """Drop disabled leaf passes from a pipeline tree."""

    disabled = {name.strip() for name in disabled_passes if str(name or "").strip()}

    def _prune(node: LLVMPipelineNode):
        if node.is_leaf:
            if node.name in disabled:
                return None
            return node

        kept = tuple(
            child
            for child in (_prune(child) for child in node.children)
            if child is not None
        )
        if not kept:
            return None
        return LLVMPipelineNode(node.name, node.params, kept)

    return tuple(
        node
        for node in (_prune(node) for node in nodes)
        if node is not None
    )


def default_pipeline_spec(opt_level: int) -> str:
    """Return the default LLVM pipeline profile string for an opt level."""
    level = max(0, min(3, int(opt_level)))
    return f"default<O{level}>"


@lru_cache(maxsize=16)
def managed_pass_names_for_spec(spec: str, opt_binary: str | None = None) -> tuple[str, ...]:
    """Return the unique concrete leaf-pass names for a pipeline spec."""
    resolved_opt = find_opt_binary(opt_binary)
    if not resolved_opt:
        return ()
    names: list[str] = []
    for pass_name in leaf_pass_names(parse_pipeline(expand_pipeline(resolved_opt, spec))):
        if pass_name not in names:
            names.append(pass_name)
    return tuple(names)


def default_profile_pass_names(opt_level: int, opt_binary: str | None = None) -> tuple[str, ...]:
    """Return the unique concrete leaf-pass names for default<O{level}>."""
    current = _llvmlite_version()
    if current == LLVM_DEFAULT_PROFILE_VERSION:
        baked = LLVM_DEFAULT_PROFILE_PASSES.get(max(0, min(3, int(opt_level))))
        if baked is not None:
            return baked
    return managed_pass_names_for_spec(default_pipeline_spec(opt_level), opt_binary)


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = _LLVM_VERSION_RE.search(text or "")
    if not match:
        return None
    parts = [int(piece) for piece in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _llvmlite_version() -> tuple[int, int, int]:
    # Bound on use, not at module scope. This module only needs llvmlite to
    # report its LLVM version when matching an external `opt` binary, but the
    # module-scope import made every importer -- including the C compile
    # driver -- depend on llvmlite being installed. A stage without it then
    # reported the whole C driver as unowned instead of naming the gap.
    import llvmlite.binding as llvm

    version = tuple(int(piece) for piece in llvm.llvm_version_info)
    if len(version) >= 3:
        return version[:3]
    return (version[0], version[1], 0)


def _normalize_opt_binary(path: str | None) -> str | None:
    if not path:
        return None
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    return shutil.which(path)


def find_opt_binary(explicit_path: str | None = None) -> str | None:
    """Return an `opt` binary whose LLVM version matches llvmlite."""
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    candidates.extend(_DEFAULT_OPT_CANDIDATES)
    seen = set()
    expected = _llvmlite_version()

    for candidate in candidates:
        resolved = _normalize_opt_binary(candidate)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        try:
            run = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if run.returncode != 0:
            continue
        version = _parse_version(run.stdout or run.stderr)
        if version == expected:
            return resolved
    return None


@lru_cache(maxsize=16)
def expand_pipeline(opt_binary: str, spec: str) -> str:
    """Expand a pipeline spec to LLVM's canonical textual form."""
    run = subprocess.run(
        [
            opt_binary,
            "-disable-output",
            f"-passes={spec}",
            "--print-pipeline-passes",
            "-",
        ],
        input=_MINIMAL_MODULE,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if run.returncode != 0:
        detail = (run.stderr or run.stdout or "unknown error").strip()
        raise RuntimeError(
            f"failed to expand LLVM pipeline {spec!r} with {opt_binary}: {detail}"
        )
    return (run.stdout or "").strip()


def run_pipeline(opt_binary: str, pipeline: str, ir_text: str) -> str:
    """Run an explicit LLVM textual pipeline through `opt` and return IR text."""
    run = subprocess.run(
        [opt_binary, "-S", f"-passes={pipeline}", "-o", "-", "-"],
        input=ir_text,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if run.returncode != 0:
        detail = (run.stderr or run.stdout or "unknown error").strip()
        raise RuntimeError(
            f"external LLVM pipeline failed with {opt_binary}: {detail}"
        )
    return run.stdout
