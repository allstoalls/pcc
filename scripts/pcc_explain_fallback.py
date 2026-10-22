#!/usr/bin/env python3
from __future__ import annotations

import argparse

from pcc.diagnostics.fallback_explainer import FallbackExplainer, explain_import


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explain pcc fallback reasons.")
    parser.add_argument("module", nargs="*")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    ns = parser.parse_args(argv)

    explainer = FallbackExplainer()
    for module in ns.module:
        reason = explain_import(module, "cpython_fallback")
        if reason is not None:
            explainer.extend([reason])
    print(explainer.format_json() if ns.format == "json" else explainer.format_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
