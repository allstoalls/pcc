"""``time.sleep`` runs natively instead of resolving through CPython.

There was no native lowering for it, so any function calling ``time.sleep`` was
replaced by a fail-closed stub under ``--python-libpython=off``. asyncio's event
loop calls it for its idle wait, which meant a loop that ever went idle died
with ``no-libpython function unavailable: asyncio._run_once`` -- the last
libpython dependency in the pcc-compiled asyncio benchmark.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap

from pcc.py_frontend.pipeline import compile_python


def _build_and_run(tmp_path: Path, name: str, source: str):
    src = tmp_path / (name + ".py")
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    exe = tmp_path / name
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    # The serial frontend is required for a worker's stub diagnostic to reach
    # stderr at all; without it the message is written inside a subprocess
    # whose stderr is discarded.
    env["PCC_DEBUG_STRICT_NOLIB_STUB"] = "1"
    env["PCC_PY_FRONTEND_JOBS"] = "1"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    return subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)


def test_time_sleep_waits_and_needs_no_libpython(tmp_path):
    result = _build_and_run(
        tmp_path,
        "time_sleep_waits",
        """
        import time


        def main() -> None:
            started = time.perf_counter()
            time.sleep(0.25)
            elapsed = time.perf_counter() - started
            print(elapsed >= 0.24)
            print(elapsed < 5.0)


        main()
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "True"]


def test_time_sleep_matches_cpython_on_zero_and_negative(tmp_path):
    source = """
        import time


        def main() -> None:
            time.sleep(0)
            print("zero ok")
            try:
                time.sleep(-1)
                print("NO ERROR")
            except ValueError as err:
                print("ValueError " + str(err))


        main()
        """
    result = _build_and_run(tmp_path, "time_sleep_edges", source)
    assert result.returncode == 0, result.stderr
    # CPython is the oracle here, message included.
    reference = subprocess.run(
        ["python3", "-c", textwrap.dedent(source)],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert reference.returncode == 0, reference.stderr
    assert result.stdout == reference.stdout
    assert result.stdout.splitlines() == [
        "zero ok",
        "ValueError sleep length must be non-negative",
    ]
