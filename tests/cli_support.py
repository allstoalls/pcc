"""Run the real standard-library CLI in an isolated, bounded process."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Mapping, Sequence

from tests.python.process_timeout import run_process_group_timeout


def run_cli(
    arguments: Sequence[str],
    *,
    entry: str = "pcc.driver.cli_launcher",
    timeout: float = 90,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if env is not None:
        environment.update(env)
    environment.pop("LC_ALL", None)
    return run_process_group_timeout(
        [sys.executable, "-m", entry, *arguments],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        timeout=timeout,
    )
