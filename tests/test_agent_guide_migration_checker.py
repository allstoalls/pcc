"""The migration checker must detect dropped text and unaccounted sections."""

from scripts.check_agent_guide_migration import (
    AUDIT, CONTRACTS, INTENT, TOOLS, coverage_problems,
)


def sample():
    bodies = {name: "Required contract " + str(i) for i, name in enumerate(CONTRACTS)}
    bodies[INTENT] = "> Preserve the project direction.\n\nAll obligations remain."
    bodies[TOOLS] = "Tool guidance.\n\n| Tool | Use |\n|---|---|\n| probe | inspect |"
    bodies["Read Next"] = "Find the actual implementation."
    baseline = "# Old guide\n\n" + "\n\n".join(
        "## " + name + "\n\n" + body for name, body in bodies.items()
    )
    documents = {
        AUDIT: "\n".join("| `" + name + "` | Moved | target |" for name in bodies),
        "docs/compiler-contract.md": "\n\n".join(
            "## " + name + "\n\n" + bodies[name] for name in CONTRACTS
        ),
        "docs/project-intent.md": "# Intent\n\nIntro.\n\n" + bodies[INTENT],
        "docs/development-tools.md": bodies[TOOLS],
    }
    return baseline, documents


def test_complete_move_is_accepted():
    baseline, documents = sample()
    assert coverage_problems(baseline, documents) == []


def test_intent_shortening_is_detected():
    baseline, documents = sample()
    documents["docs/project-intent.md"] = "# Intent\n\n> Preserve the project direction."
    assert "Project Intent body is not a verbatim move" in coverage_problems(baseline, documents)


def test_weakened_contract_is_detected():
    baseline, documents = sample()
    documents["docs/compiler-contract.md"] = documents["docs/compiler-contract.md"].replace(
        "Required contract 1", "Optional contract 1"
    )
    assert "contract is not a verbatim move: " + CONTRACTS[1] in coverage_problems(baseline, documents)


def test_missing_or_duplicate_accounting_is_detected():
    baseline, documents = sample()
    documents[AUDIT] = documents[AUDIT].replace("| `Read Next` | Moved | target |", "")
    documents[AUDIT] += "\n| `" + INTENT + "` | Moved | target |"
    problems = coverage_problems(baseline, documents)
    assert "section must be accounted for exactly once: Read Next" in problems
    assert "section must be accounted for exactly once: " + INTENT in problems


def test_dropped_tool_is_detected():
    baseline, documents = sample()
    documents["docs/development-tools.md"] = "| Tool | Use |\n|---|---|"
    assert "original development-tool table changed" in coverage_problems(baseline, documents)
