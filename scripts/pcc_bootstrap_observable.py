#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from pcc.driver.cli_observability import (
    normalize_diagnostic_format,
    profile_scope,
    write_exception_diagnostic,
)
from pcc.frontends.python.pipeline import compile_python


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Python compiler with diagnostics/profile JSON.")
    parser.add_argument("path")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--emit-llvm", action="store_true")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--python-libpython", default=None)
    parser.add_argument("--ir-scaffold", default=None)
    parser.add_argument("--diagnostic-format", default="text")
    parser.add_argument("--profile-json", default=None)
    ns = parser.parse_args(argv)

    fmt = normalize_diagnostic_format(ns.diagnostic_format)
    with profile_scope(ns.profile_json, command="compile_python",
                       metadata={"path": ns.path, "output": ns.output}):
        try:
            compile_python(
                ns.path,
                ns.output,
                emit_llvm_only=ns.emit_llvm,
                libpython_mode=ns.python_libpython,
                ir_scaffold_mode=ns.ir_scaffold,
                backend=ns.backend,
                recursive_stdlib=False,
            )
        except Exception as exc:
            write_exception_diagnostic(
                exc, fmt=fmt, phase="python-pipeline",
                code="PCC-PY-PIPELINE-001",
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
