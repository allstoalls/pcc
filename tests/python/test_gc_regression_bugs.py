"""GC regression bug fixture — pinned reproducers for specific
historical GC bugs in pcc.

Pattern modeled after CPython's `test_bug<NNNN>_*` tests in
`Lib/test/test_gc.py`. Every entry in this file is a reduced
test case for a specific concrete bug that was hit, fixed (or in the
process of being fixed). Once a bug is fixed, its test must keep
passing in perpetuity to prevent regression.

Naming: `test_<short_id>_<short_description>`. Cross-link to the
corresponding task in `tasks.md` via the docstring's "Tracked as:"
line.

DO NOT remove tests from this file even after the corresponding bug
is fixed — they're the regression net.
"""
from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120,
    )


def _compile_and_run_capture_rss(
    tmp_path, source: str,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run binary with /usr/bin/time -l and return (proc, peak_rss_kib)."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")

    proc = subprocess.run(
        ["/usr/bin/time", "-l", str(exe)],
        capture_output=True, text=True, timeout=300,
    )
    rss_kib = -1
    for line in proc.stderr.splitlines():
        if "maximum resident set size" in line.lower():
            try:
                v = int(line.strip().split()[0])
                rss_kib = v // 1024 if v > 1_000_000 else v
            except (ValueError, IndexError):
                pass
            break
    main_proc = subprocess.CompletedProcess(
        args=[str(exe)],
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr="",
    )
    return main_proc, rss_kib


# ---------------------------------------------------------------------------
# BUG #110: py_str_new in function scope leaks linearly
# ---------------------------------------------------------------------------


@pytest.mark.pcc_gate(
    unavailable=None
    if os.path.exists("/usr/bin/time")
    else "needs /usr/bin/time -l for RSS measurement"
)
def test_bug_110_str_in_local_list_does_not_leak(tmp_path):
    """Function builds a 100-string local list, returns None, gets
    called 100k times. RSS should plateau, not climb.

    Discovery: 2026-05-02 by tests/test_gc_effectiveness.py while
    writing the GC effectiveness contract. RSS observed at 1.5 GB
    (100k iter) and 15 GB (1M iter) — strictly linear, confirming
    leak. Threshold of 200 MB is the post-fix expectation.
    """
    result, rss_kib = _compile_and_run_capture_rss(tmp_path, """
        def make_drop():
            xs = []
            i: int = 0
            while i < 100:
                xs.append("v" + str(i))
                i = i + 1
            return None

        def main() -> None:
            n: int = 100_000
            i: int = 0
            while i < n:
                make_drop()
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    if rss_kib > 0:
        assert rss_kib < 200_000, (
            f"BUG #110 still active: RSS {rss_kib} KiB (~{rss_kib//1024} MiB)"
        )


# ---------------------------------------------------------------------------
# Sentinel: this file is never empty
# ---------------------------------------------------------------------------


def test_regression_fixture_present():
    """Trivial sentinel so pytest collects this file even when no
    bugs are open. When a real bug is added, this can be deleted."""
    assert True
