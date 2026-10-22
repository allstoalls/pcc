#!/usr/bin/env python3
"""Check that layer1.py remains a small facade.

This is the gate for goal.md No.17.  The heavy lowering logic should live in
split mixin modules, while layer1.py only exposes the public L1CodeGen class
and compatibility constants.
"""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
LAYER1 = ROOT / "pcc" / "frontends" / "python" / "codegen" / "layer1.py"
OWNERSHIP_DOC = ROOT / "docs" / "architecture" / "layer1-ownership.md"
MIXIN_STACK = ROOT / "pcc" / "frontends" / "python" / "codegen" / "layer1_mixins.py"


FORBIDDEN_SNIPPETS = (
    "def _emit_",
    "def _maybe_emit_",
    "def _lower_",
    "py_gc_",
    "py_thread",
    "py_coroutine",
)


def fail(message: str) -> int:
    print(f"layer1 ownership check failed: {message}", file=sys.stderr)
    return 1


def main() -> int:
    if not LAYER1.exists():
        return fail(f"missing {LAYER1.relative_to(ROOT)}")
    if not MIXIN_STACK.exists():
        return fail(f"missing {MIXIN_STACK.relative_to(ROOT)}")
    if not OWNERSHIP_DOC.exists():
        return fail(f"missing {OWNERSHIP_DOC.relative_to(ROOT)}")

    text = LAYER1.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) > 200:
        return fail(f"layer1.py has {len(lines)} lines; expected <= 200")

    for snippet in FORBIDDEN_SNIPPETS:
        if snippet in text:
            return fail(f"layer1.py contains implementation snippet {snippet!r}")

    required_modules = (
        "native_gc.py",
        "native_threading.py",
        "native_asyncio.py",
        "native_modules.py",
        "generator_lowering.py",
        "async_with_lowering.py",
    )
    missing = [
        name
        for name in required_modules
        if not (ROOT / "pcc" / "frontends" / "python" / "codegen" / name).exists()
    ]
    if missing:
        return fail("missing split lowering modules: " + ", ".join(missing))

    print("layer1 ownership check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
