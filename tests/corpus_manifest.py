"""Keep every runtime observation connected to a corpus comparison."""
from __future__ import annotations


def runtime_cases_except(manifest, handled_categories):
    """Select the remaining runtime cases, including future failure categories.

    A classification records an earlier observation; it must not silently remove
    a program from collection. Callers run these cases through their ordinary
    phase-aware exact-result comparison, which reports build/launch/timeout
    failures before comparing actual program exits and both output streams.
    Only an explicitly classified returncode-only bucket may ignore output.
    """
    return [
        case
        for category, cases in manifest.items()
        if category.startswith("runtime_") and category not in handled_categories
        for case in cases
    ]
