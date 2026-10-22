#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from pcc.diagnostics.ir_diff import diff_ir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Structural diff for LLVM IR files")
    parser.add_argument("lhs")
    parser.add_argument("rhs")
    args = parser.parse_args(argv)
    diff = diff_ir(
        Path(args.lhs).read_text(encoding="utf-8"),
        Path(args.rhs).read_text(encoding="utf-8"),
    )
    text = diff.to_text()
    if text:
        print(text)
    return 1 if not diff.is_empty() else 0


if __name__ == "__main__":
    raise SystemExit(main())
