"""Read-only checks for the 2026-09-08 agent-guide migration.

Run from a checkout retaining BASELINE in git history. These checks establish
coverage accounting and exact moves, not semantic equivalence of paraphrases.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import sys

BASELINE = "d87f85940fe8fc6301f428f74e9b89bdfbac061a"
BASELINE_SHA256 = "08cb942d6f9f706074433a7d4e23d2d976068b1cfbe3b93953d9cc599f9c5202"
INTENT = "Project Intent (north star — read before changing direction)"
CONTRACTS = (
    "Dependency ownership contract (maintainer directive, 2026-09-07)",
    "CPython library contract (maintainer directive, 2026-09-08)",
    "Public CLI parity contract (maintainer directive, 2026-09-08)",
)
TOOLS = "Dev Tools — check here before writing a probe"
AUDIT = "docs/agents-migration-audit.md"
DOCUMENTS = (
    "AGENTS.md", "docs/project-intent.md", "docs/compiler-contract.md",
    "docs/development-tools.md", "docs/validation-workflow.md",
    "docs/debugging-playbook.md", "docs/investigation-workflow.md",
    "docs/knowledge/README.md", "docs/knowledge/2026-09-08-optimizer-pipeline-audit.md",
    AUDIT,
)


def sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## (.+)\n", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|")]


def coverage_problems(baseline: str, documents: dict[str, str]) -> list[str]:
    old = sections(baseline)
    mapped = re.findall(r"^\| `([^`]+)` \|", documents[AUDIT], flags=re.MULTILINE)
    problems = []
    for name in old:
        if mapped.count(name) != 1:
            problems.append(f"section must be accounted for exactly once: {name}")
    for name in mapped:
        if name not in old:
            problems.append(f"unknown baseline section: {name}")
    current_contracts = sections(documents["docs/compiler-contract.md"])
    for name in CONTRACTS:
        if name not in old or current_contracts.get(name) != old[name]:
            problems.append(f"contract is not a verbatim move: {name}")
    intent = documents["docs/project-intent.md"]
    # The historical body starts with a blockquote; only a clearly separate
    # introduction may precede it. No omitted or appended body is accepted.
    marker = "\n> "
    start = intent.find(marker)
    if INTENT not in old or start < 0 or intent[start:].strip() != old[INTENT]:
        problems.append("Project Intent body is not a verbatim move")
    if TOOLS not in old or table_rows(documents["docs/development-tools.md"]) != table_rows(old[TOOLS]):
        problems.append("original development-tool table changed")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        ["git", "show", BASELINE + ":AGENTS.md"], cwd=root,
        capture_output=True, timeout=15,
    )
    if done.returncode:
        print("Cannot read the pinned baseline; retain that commit in local git history.", file=sys.stderr)
        return 1
    if hashlib.sha256(done.stdout).hexdigest() != BASELINE_SHA256:
        print("Baseline content hash mismatch", file=sys.stderr)
        return 1
    try:
        documents = {name: (root / name).read_text() for name in DOCUMENTS}
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    baseline = done.stdout.decode()
    problems = coverage_problems(baseline, documents)
    links = 0
    for name, text in documents.items():
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith(("https:", "http:", "mailto:", "#")):
                continue
            links += 1
            if not ((root / name).parent / target.split("#", 1)[0]).exists():
                problems.append(f"missing local link in {name}: {target}")
    if problems:
        for problem in problems:
            print("FAIL:", problem, file=sys.stderr)
        return 1
    print(f"PASS: pinned baseline hash; {len(sections(baseline))} sections accounted for")
    print("PASS: Project Intent and 3 maintainer contracts preserved verbatim")
    print(f"PASS: original tool table preserved; {links} local link paths exist")
    print("LIMIT: paraphrase semantics and agent compliance require review; see " + AUDIT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
