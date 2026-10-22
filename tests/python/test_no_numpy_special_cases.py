from __future__ import annotations

import re
from pathlib import Path

from pcc1_gate import repo_root

REPO = repo_root()


FORBIDDEN_NEEDLES = (
    "PY_TYPE_NUMPY_ARRAY",
    "py_dealloc_numpy_array",
    "py_numpy.c",
    "numpy.array",
)

FORBIDDEN_TORCH_NEEDLES = (
    "PY_TYPE_TORCH",
    "THPTensor",
    "py_torch.c",
    "torch.tensor",
)

FORBIDDEN_MLX_NEEDLES = (
    "PY_TYPE_MLX_ARRAY",
    "PyMlxArrayObject",
    "py_mlx.c",
    "mlx.array",
    "mlx_compat",
)

FORBIDDEN_VLLM_NEEDLES = (
    "PY_TYPE_VLLM",
    "PyVllmObject",
    "PyVllmMetalObject",
    "py_vllm.c",
    "py_vllm_metal.c",
    "vllm_compat",
    "vllm_metal_compat",
)

FORBIDDEN_TILELANG_NEEDLES = (
    "PY_TYPE_TILELANG",
    "PyTileLangObject",
    "py_tilelang.c",
    "tilelang.jit",
    "tilelang_compat",
)

FORBIDDEN_BRANCH_PATTERNS = (
    re.compile(r"\bif\s+package\s*==\s*['\"]numpy['\"]"),
    re.compile(r"\bif\s+package_name\s*==\s*['\"]numpy['\"]"),
    re.compile(r"\bif\s+name\s*==\s*['\"]numpy['\"]"),
    re.compile(r"\b(?:if|elif)\s+profile\s*(?:==|!=)\s*['\"]numpy-core-l6['\"]"),
)

FORBIDDEN_TORCH_BRANCH_PATTERNS = (
    re.compile(r"\bif\s+package\s*==\s*['\"](?:torch|pytorch)['\"]"),
    re.compile(r"\bif\s+package_name\s*==\s*['\"](?:torch|pytorch)['\"]"),
    re.compile(r"\bif\s+name\s*==\s*['\"](?:torch|pytorch)['\"]"),
    re.compile(r"\bif\s+module\.startswith\(\s*['\"]torch\.['\"]\s*\)"),
    re.compile(r"\bif\s+name\.startswith\(\s*['\"]torch\.['\"]\s*\)"),
)

FORBIDDEN_MLX_BRANCH_PATTERNS = (
    re.compile(r"\bif\s+package\s*==\s*['\"]mlx['\"]"),
    re.compile(r"\bif\s+package_name\s*==\s*['\"]mlx['\"]"),
    re.compile(r"\bif\s+name\s*==\s*['\"]mlx['\"]"),
    re.compile(r"\bif\s+module\.startswith\(\s*['\"]mlx\.['\"]\s*\)"),
    re.compile(r"\bif\s+name\.startswith\(\s*['\"]mlx\.['\"]\s*\)"),
)

FORBIDDEN_VLLM_BRANCH_PATTERNS = (
    re.compile(r"\bif\s+package\s*==\s*['\"]vllm['\"]"),
    re.compile(r"\bif\s+package\s*==\s*['\"]vllm[-_]metal['\"]"),
    re.compile(r"\bif\s+package_name\s*==\s*['\"]vllm['\"]"),
    re.compile(r"\bif\s+package_name\s*==\s*['\"]vllm[-_]metal['\"]"),
    re.compile(r"\bif\s+name\s*==\s*['\"]vllm['\"]"),
    re.compile(r"\bif\s+name\s*==\s*['\"]vllm[-_]metal['\"]"),
    re.compile(r"\bif\s+module\.startswith\(\s*['\"]vllm\.['\"]\s*\)"),
    re.compile(r"\bif\s+module\.startswith\(\s*['\"]vllm_metal\.['\"]\s*\)"),
    re.compile(r"\bif\s+name\.startswith\(\s*['\"]vllm\.['\"]\s*\)"),
    re.compile(r"\bif\s+name\.startswith\(\s*['\"]vllm_metal\.['\"]\s*\)"),
)

FORBIDDEN_TILELANG_BRANCH_PATTERNS = (
    re.compile(r"\bif\s+package\s*==\s*['\"]tilelang['\"]"),
    re.compile(r"\bif\s+package_name\s*==\s*['\"]tilelang['\"]"),
    re.compile(r"\bif\s+name\s*==\s*['\"]tilelang['\"]"),
    re.compile(r"\bif\s+module\.startswith\(\s*['\"]tilelang\.['\"]\s*\)"),
    re.compile(r"\bif\s+name\.startswith\(\s*['\"]tilelang\.['\"]\s*\)"),
)

SCAN_ROOTS = (
    REPO / "pcc" / "frontends" / "python",
    REPO / "pcc" / "runtime",
    REPO / "pcc" / "frontends" / "c" / "codegen",
    REPO / "pcc" / "package",
    REPO / "pcc" / "array_core.py",
    REPO / "pcc" / "package" / "compat.py",
    REPO / "pcc" / "driver" / "cli_bootstrap.py",
)

TORCH_METADATA_MENTION_ALLOWLIST = {
    Path("pcc/package/compat.py"),
    Path("pcc/package/campaign.py"),
}


def _iter_scanned_files():
    for root in SCAN_ROOTS:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".c", ".h"}:
                yield path


def test_compiler_and_runtime_do_not_special_case_numpy():
    offenders: list[str] = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(REPO)
        for needle in FORBIDDEN_NEEDLES:
            if needle in text:
                offenders.append(f"{rel}: contains {needle!r}")
        for pattern in FORBIDDEN_BRANCH_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{rel}: contains NumPy package branch")
    assert not offenders, "\n".join(offenders)


def test_compiler_and_runtime_do_not_special_case_torch():
    offenders: list[str] = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(REPO)
        lowered = text.lower()
        if rel not in TORCH_METADATA_MENTION_ALLOWLIST:
            if "pytorch" in lowered:
                offenders.append(f"{rel}: contains 'pytorch'")
            if "torch" in lowered:
                offenders.append(f"{rel}: contains 'torch'")
        for needle in FORBIDDEN_TORCH_NEEDLES:
            if needle in text:
                offenders.append(f"{rel}: contains {needle!r}")
        for pattern in FORBIDDEN_TORCH_BRANCH_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{rel}: contains torch package branch")
    assert not offenders, "\n".join(offenders)


def test_compiler_and_runtime_do_not_special_case_mlx():
    offenders: list[str] = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(REPO)
        for needle in FORBIDDEN_MLX_NEEDLES:
            if needle in text:
                offenders.append(f"{rel}: contains {needle!r}")
        for pattern in FORBIDDEN_MLX_BRANCH_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{rel}: contains MLX package branch")
    assert not offenders, "\n".join(offenders)


def test_compiler_and_runtime_do_not_special_case_vllm():
    offenders: list[str] = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(REPO)
        for needle in FORBIDDEN_VLLM_NEEDLES:
            if needle in text:
                offenders.append(f"{rel}: contains {needle!r}")
        for pattern in FORBIDDEN_VLLM_BRANCH_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{rel}: contains vLLM package branch")
    assert not offenders, "\n".join(offenders)


def test_compiler_and_runtime_do_not_special_case_tilelang():
    offenders: list[str] = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(REPO)
        for needle in FORBIDDEN_TILELANG_NEEDLES:
            if needle in text:
                offenders.append(f"{rel}: contains {needle!r}")
        for pattern in FORBIDDEN_TILELANG_BRANCH_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{rel}: contains TileLang package branch")
    assert not offenders, "\n".join(offenders)
