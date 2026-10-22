"""Line information for Python-frontend builds.

The Python frontend emitted no debug info at all, so a pcc-compiled program
could only be debugged by rebuilding it with print statements.  These tests
pin the three things that made the feature silently produce nothing during
development: the flag has to survive the CLI's Python branch, the locations
have to survive the IR pass pipeline, and the metadata has to be valid enough
that LLVM keeps the compile unit instead of discarding it with a warning.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.python import pcc1_gate


SOURCE = """def add(a, b):
    t = a + b
    return t

print(add(3, 4))
"""


def _compile_ll(tmp_path, *, debug: bool):
    src = tmp_path / ("dbg_on.py" if debug else "dbg_off.py")
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / (src.stem + ".ll")
    argv = ["-g"] if debug else []
    argv += ["--emit-llvm=" + str(out), str(src)]
    env = dict(os.environ)
    env.pop("PCC_PY_DEBUG_INFO", None)
    env.pop("LC_ALL", None)
    result = subprocess.run(
        [os.sys.executable, "-m", "pcc", *argv],
        cwd=str(pcc1_gate.repo_root()),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return out.read_text(encoding="utf-8")


def test_debug_flag_emits_locations(tmp_path):
    """``-g`` has to reach the frontend through the CLI's Python branch.

    That branch returns before the C path's option normalization, so a flag
    wired only into the later block produced no debug info at all while every
    direct call looked correct.
    """
    text = _compile_ll(tmp_path, debug=True)
    assert "DICompileUnit" in text
    assert "llvm.dbg.cu" in text
    assert 'name: "add"' in text
    # The function body spans source lines 2 and 3.
    lines = {
        int(part.split(",")[0])
        for chunk in text.split("!DILocation(")[1:]
        for part in [chunk.split("line: ", 1)[1]]
    }
    assert {2, 3} <= lines, lines


def test_without_flag_nothing_changes(tmp_path):
    text = _compile_ll(tmp_path, debug=False)
    assert "!dbg" not in text
    assert "DILocation" not in text
    assert "DISubprogram" not in text


def test_locations_survive_the_ir_pass_pipeline(tmp_path):
    """mem2reg/sroa rewrite instruction lines and drop their ``!dbg`` suffix.

    Locations pointing at instructions that no longer exist are worse than no
    line table, because they look correct.  A debug build therefore skips the
    owned transforms, the same way ``-g`` implies ``-O0`` elsewhere.
    """
    text = _compile_ll(tmp_path, debug=True)
    assert text.count("!dbg") > 0


# clang not installed
@pytest.mark.pcc_gate(probe=lambda: not (shutil.which("clang") is None))
def test_llvm_accepts_the_metadata_and_emits_dwarf(tmp_path):
    """LLVM drops the whole compile unit on invalid metadata, with a warning.

    Two shapes did exactly that during development: a missing
    ``Debug Info Version`` module flag, and a ``DILocation`` whose scope was a
    ``DIFile`` rather than a subprogram.
    """
    ll = tmp_path / "dbg.ll"
    ll.write_text(_compile_ll(tmp_path, debug=True), encoding="utf-8")
    obj = tmp_path / "dbg.o"
    compiled = subprocess.run(
        ["clang", "-c", str(ll), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert compiled.returncode == 0, compiled.stderr[-2000:]
    assert "invalid version" not in compiled.stderr
    assert "requires a valid scope" not in compiled.stderr

    dumped = subprocess.run(
        ["dwarfdump", "--debug-line", str(obj)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert "end_sequence" in dumped.stdout, dumped.stdout[-2000:]
