#!/usr/bin/env python3
"""Release each compiler process's heap before the next native phase.

usage: run_pcc_native_deferred.py COMPILER CODEGEN_PLAN LINK_PLAN -- COMMAND...

The bootstrap runs the frontend coordinator as ``COMMAND`` and then resumes its
codegen or link plan in a fresh process of the same compiler, so the
coordinator's heap is gone before the deferred phase starts.  The compiler
must advertise ``--pcc-native-deferred-worker``; a compiler that cannot run
the continuation fails here instead of silently falling back to host Python.

Python port of ``scripts/run_pcc_native_deferred.sh``: no shell, and the
continuation is the same executable on every platform (Windows included).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_USAGE = (
    "usage: run_pcc_native_deferred.py COMPILER CODEGEN_PLAN LINK_PLAN -- COMMAND..."
)


def _plan_to_resume(codegen_plan: str, link_plan: str) -> str:
    if codegen_plan and Path(codegen_plan).is_file():
        return codegen_plan
    if link_plan and Path(link_plan).is_file():
        return link_plan
    return ""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 5 or argv[3] != "--":
        print(_USAGE, file=sys.stderr)
        return 2
    compiler, codegen_plan, link_plan = argv[0], argv[1], argv[2]
    command = argv[4:]

    check = subprocess.run(
        [compiler, "--pcc-native-deferred-worker", "--check"], check=False
    )
    if check.returncode != 0:
        print(
            "native deferred execution is unavailable in "
            + compiler
            + "; rebuild stage1 from current sources (host Python fallback is forbidden)",
            file=sys.stderr,
        )
        return 2

    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        return completed.returncode

    plan = _plan_to_resume(codegen_plan, link_plan)
    if not plan:
        return 0
    # Replace this process, exactly as the shell wrapper's ``exec`` did: the
    # continuation owns the exit status and the wrapper adds no frame.
    os.execv(compiler, [compiler, "--pcc-native-deferred-worker", plan])
    return 0  # pragma: no cover - os.execv never returns


if __name__ == "__main__":
    raise SystemExit(main())
