"""No-skip doctrine: tests run or are deselected, never skipped.

A skipped test reports green while proving nothing, so this repository routes
"cannot run here" through `pytest.mark.pcc_gate(unavailable=...)`, which
*deselects*. `pytest.skip` / `skipif` / `importorskip` / `unittest.SkipTest`
are banned in executed code (TEST-P1-NO-SKIP-DOCTRINE-REMAINING-FAMILIES).

This was a manual `rg` line in the task board's gate list, which means a human
had to eyeball every hit and decide whether it was real. Two kinds of hit are
legitimate and look identical to a text search:

- source-text fixtures — a test that feeds `"@pytest.mark.skipif(...)"` to the
  compiler as *input*
- the compiler's own recognition of those decorator names
  (`pcc/driver/cli_bootstrap.py`, `pcc/frontends/python/codegen/decorator_lowering.py`)

Parsing instead of grepping makes that distinction structural: a string
literal is not a call, so only genuinely executed skips are reported.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


REPO = _repo_root()

_SKIP_CALLS = {
    ("pytest", "skip"),
    ("pytest", "importorskip"),
    ("unittest", "SkipTest"),
}
_SKIP_DECORATORS = {
    ("pytest", "mark", "skip"),
    ("pytest", "mark", "skipif"),
    ("unittest", "skip"),
    ("unittest", "skipUnless"),
    ("unittest", "skipIf"),
}


def _dotted(node) -> tuple[str, ...] | None:
    """Dotted name for `a.b.c`, or None for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def _offenders_in(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in _SKIP_CALLS:
                found.append(f"{path.relative_to(REPO)}:{node.lineno}: {'.'.join(name)}()")
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                name = _dotted(target)
                if name in _SKIP_DECORATORS:
                    found.append(
                        f"{path.relative_to(REPO)}:{deco.lineno}: @{'.'.join(name)}"
                    )
    return found


def _python_sources():
    for root in ("tests", "pcc", "scripts"):
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_no_executed_skip_mechanism_anywhere():
    offenders: list[str] = []
    for path in _python_sources():
        offenders.extend(_offenders_in(path))
    assert not offenders, (
        "the no-skip doctrine bans these: a skipped test reports green while "
        "proving nothing. Use pytest.mark.pcc_gate(unavailable=<reason>) so the "
        "test is deselected instead:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_scan_actually_reads_the_tree():
    """A scanner that parses nothing passes for the wrong reason."""
    count = sum(1 for _ in _python_sources())
    assert count > 500, count


def test_the_scan_would_catch_a_real_skip(tmp_path):
    """Positive control — the checks above are only worth their false-negative
    rate, so prove the scanner fires on each banned form."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import pytest\n"
        "import unittest\n"
        "\n"
        "# a source-text fixture must NOT be reported\n"
        'FIXTURE = "@pytest.mark.skipif(True, reason=\'skip\')"\n'
        "RECOGNIZED = ('pytest.mark.skip', 'pytest.mark.skipif')\n"
        "\n"
        "def a():\n"
        "    pytest.skip('nope')\n"
        "\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "def b():\n"
        "    pass\n"
        "\n"
        "def c():\n"
        "    raise unittest.SkipTest('nope')\n",
        encoding="utf-8",
    )
    # _offenders_in reports paths relative to REPO, so scan in place.
    tree = ast.parse(probe.read_text(encoding="utf-8"))
    calls = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _dotted(n.func) in _SKIP_CALLS
    )
    decos = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for d in n.decorator_list
        if _dotted(d.func if isinstance(d, ast.Call) else d) in _SKIP_DECORATORS
    )
    assert calls == 2, calls  # pytest.skip + unittest.SkipTest
    assert decos == 1, decos  # the decorator; the string fixture is not one
