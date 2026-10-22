from __future__ import annotations

import subprocess
import sys

from pcc.support.threading_compat import SCENARIOS, by_name, scenario_names


def _run_host(source: str):
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_threading_compat_scenarios_are_unique_and_nontrivial():
    names = scenario_names()
    assert len(names) == len(set(names))
    assert "thread-start-join" in names
    for scenario in SCENARIOS:
        assert "print(" in scenario.source
        assert scenario.expected_stdout


def test_threading_compat_host_outputs_match_expectations():
    for scenario in SCENARIOS:
        result = _run_host(scenario.source)
        assert result.returncode == 0, (scenario.name, result.stderr)
        assert result.stdout.strip().splitlines() == list(scenario.expected_stdout)


def test_threading_compat_lookup_by_name():
    assert by_name("lock-event").expected_stdout == ("True", "False", "True")
