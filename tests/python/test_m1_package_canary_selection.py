from __future__ import annotations

import json
import io
import re
import tokenize
from pathlib import Path

# tests/conftest.py intentionally rewrites Path.resolve() for legacy test
# layouts.  absolute() keeps this test anchored to the real repository root.
REPO = Path(__file__).absolute().parents[2]
PIN_PATH = REPO / "docs" / "goal" / "m1-package-canary.json"
REPORT_PATH = REPO / "docs" / "reports" / "m1-package-canary-selection.md"
DISPATCH_ROOTS = (
    REPO / "pcc" / "frontends" / "python",
    REPO / "pcc" / "runtime",
    REPO / "pcc" / "frontends" / "c" / "codegen",
    REPO / "pcc" / "package",
    REPO / "pcc" / "array_core.py",
    REPO / "pcc" / "package" / "compat.py",
    REPO / "pcc" / "driver" / "cli_bootstrap.py",
)


def _pin() -> dict[str, object]:
    return json.loads(PIN_PATH.read_text(encoding="utf-8"))


def _iter_dispatch_files():
    for root in DISPATCH_ROOTS:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".c", ".h"}:
                yield path


def _dispatch_text(path: Path) -> str:
    """Return executable/source tokens while excluding comments.

    The canary guard is meant to catch package names in dispatch conditions,
    tables, and string literals.  Documentation comments are evidence about a
    mechanism, not a package special case, and must not create false positives.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".py":
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return tokenize.untokenize(
            token for token in tokens if token.type != tokenize.COMMENT
        )
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.DOTALL)


def test_m1_canary_pin_is_real_bounded_and_pep489():
    pin = _pin()
    selected = pin["selected"]
    candidates = pin["candidates"]

    assert pin["schema"] == "pcc.m1-package-canary.v1"
    assert pin["probe"]["timebox_hours"] == 48
    assert len(candidates) >= 3
    assert selected["distribution"] not in {"fixture", "synthetic"}
    assert selected["version"]
    assert selected["source_url"].startswith("https://files.pythonhosted.org/")
    assert selected["source_url"].endswith(selected["source_archive"])
    assert re.fullmatch(r"[0-9a-f]{64}", selected["sha256"])
    assert selected["init_contract"]["kind"] in {"pep-489", "pytype-from-spec"}
    assert "Py_mod_exec" in selected["init_contract"]["markers"]
    assert "PyModuleDef_Init" in selected["init_contract"]["markers"]

    selected_rows = [row for row in candidates if row["selection"] == "selected"]
    assert len(selected_rows) == 1
    assert selected_rows[0]["distribution"] == selected["distribution"]
    assert selected_rows[0]["sha256"] == selected["sha256"]


def test_m1_canary_report_checks_every_first_phase_boundary():
    pin = _pin()
    selected = pin["selected"]
    boundaries = pin["selected_first_boundaries"]
    report = REPORT_PATH.read_text(encoding="utf-8")

    assert set(boundaries) == {"build", "link", "module_init", "behavior"}
    assert boundaries["build"]["status"] == "blocked"
    assert boundaries["link"]["status"] == "not_reached"
    assert boundaries["module_init"]["status"] == "not_reached"
    assert boundaries["behavior"]["status"] == "not_reached"
    for boundary in boundaries.values():
        assert boundary["first_blocker"]

    for value in (
        selected["distribution"],
        selected["version"],
        selected["source_url"],
        selected["sha256"],
        "PyUnicodeWriter",
        "Py_mod_exec",
    ):
        assert value in report


def test_selected_canary_name_is_absent_from_compiler_runtime_dispatch():
    selected_name = _pin()["selected"]["distribution"].lower()
    offenders: list[str] = []
    for path in _iter_dispatch_files():
        text = _dispatch_text(path).lower()
        if selected_name in text:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, "selected canary leaked into dispatch:\n" + "\n".join(
        offenders
    )
