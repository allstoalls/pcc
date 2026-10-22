"""PCC_DEBUG_BOOTSTRAP_TRACE is a general tool, not a py_lift-only one.

`docs/debugging-playbook.md` documents the variable as a general codegen
probe, but the probe sites were wrapped in
``if self.module.name == "pcc.frontends.python.py_lift":``, so setting it did nothing
for any other module (AUD-P2-SELF-MODULE-SPECIAL-CASES-IN-CODEGEN). The
module filter now lives in the variable itself.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from pcc.frontends.python.codegen.bootstrap_trace import bootstrap_trace_enabled


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PCC_DEBUG_BOOTSTRAP_TRACE", raising=False)


def test_unset_or_empty_disables_tracing(monkeypatch):
    assert not bootstrap_trace_enabled("pcc.frontends.python.py_lift")
    monkeypatch.setenv("PCC_DEBUG_BOOTSTRAP_TRACE", "   ")
    assert not bootstrap_trace_enabled("pcc.frontends.python.py_lift")


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "all", "*"])
def test_truthy_values_trace_every_module(monkeypatch, value):
    monkeypatch.setenv("PCC_DEBUG_BOOTSTRAP_TRACE", value)
    assert bootstrap_trace_enabled("pcc.frontends.python.py_lift")
    assert bootstrap_trace_enabled("pcc.frontends.python.codegen.layer1")
    assert bootstrap_trace_enabled(None)


def test_a_module_name_restricts_tracing_to_that_module(monkeypatch):
    monkeypatch.setenv("PCC_DEBUG_BOOTSTRAP_TRACE", "pcc.frontends.python.py_lift")
    assert bootstrap_trace_enabled("pcc.frontends.python.py_lift")
    assert not bootstrap_trace_enabled("pcc.frontends.python.codegen.layer1")


def test_comma_separated_prefixes_are_honored(monkeypatch):
    monkeypatch.setenv(
        "PCC_DEBUG_BOOTSTRAP_TRACE", " pcc.frontends.python.py_lift , pcc.frontends.python.codegen.layer1 "
    )
    assert bootstrap_trace_enabled("pcc.frontends.python.py_lift")
    assert bootstrap_trace_enabled("pcc.frontends.python.codegen.layer1")
    assert not bootstrap_trace_enabled("pcc.backend.self_backend_emit")


def test_no_codegen_probe_is_hardcoded_to_a_single_module():
    """The name-keyed gate must not come back."""
    import ast

    codegen = _repo_root() / "pcc" / "frontends" / "python" / "codegen"
    offenders = []
    for path in codegen.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `self.module.name == "pcc.frontends.python.py_lift"` as real code, not as
            # prose: bootstrap_trace.py documents the old idiom in its
            # docstring, which is a description rather than a gate.
            if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
                continue
            right = node.comparators[0]
            if not (isinstance(right, ast.Constant) and right.value == "pcc.frontends.python.py_lift"):
                continue
            left = node.left
            if (
                isinstance(left, ast.Attribute)
                and left.attr == "name"
                and isinstance(left.value, ast.Attribute)
                and left.value.attr == "module"
            ):
                offenders.append(path.name)
                break
    assert not offenders, (
        "codegen probes are gated on a hardcoded module name again: "
        + ", ".join(sorted(offenders))
    )
