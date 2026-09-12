"""pcc1 compiles C itself, in this process, with no host pcc behind it.

The rest of ``tests/c`` runs the host compiler, so it says nothing about the
self-hosted binary: pcc1 used to fail closed on a C input with
``PCC-CPY-UNSUPPORTED-L3-TOOLING-C-DRIVER`` and hand the work to a child host
pcc, which meant pointing a test at pcc1 still measured host pcc.

Delegation is ruled out rather than assumed.  ``PCC_HOST_PCC`` and
``PCC_HOST_PYTHON`` are pointed at paths that cannot exist, so a build that
tries to shell out fails, and only an in-process compile can succeed.

``cc`` is the oracle for what the compiled program must print.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).absolute().parents[2]
_PCC1_CANDIDATES = [
    REPO / "build" / "bootstrap" / "pcc1",
    REPO / "build" / "bootstrap-pytest-self" / "pcc1",
    REPO / "build" / "bootstrap-self-claude" / "pcc1",
    REPO / "build" / "bootstrap-strict-self" / "pcc1",
    REPO / "build" / "bootstrap-self-darwin_arm64" / "pcc1",
]


def _find_pcc1() -> Path | None:
    env_path = os.environ.get("PCC1_BINARY")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate
    for candidate in _PCC1_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


PCC1 = _find_pcc1()
pytestmark = pytest.mark.pcc_gate(probe="pcc1")


_PROGRAM = """\
#include <stdio.h>

static int add(int a, int b) { return a + b; }

struct point {
    int x;
    int y;
};

int main(void) {
    int values[3] = {20, 22, 0};
    values[2] = add(values[0], values[1]);
    printf("%d\\n", values[2]);
    struct point p = {3, 4};
    printf("%d\\n", p.x * p.y);
    for (int i = 0; i < 3; i++) {
        printf("v%d=%d\\n", i, values[i]);
    }
    return 0;
}
"""


def _no_host_delegation_env() -> dict:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    # Any attempt to hand the C input to a host driver has to fail: these two
    # are the only routes ``cli_bootstrap._run_c_cli`` has out of the process.
    env["PCC_HOST_PCC"] = str(REPO / "build" / "does-not-exist-host-pcc")
    env["PCC_HOST_PYTHON"] = str(REPO / "build" / "does-not-exist-host-python")
    return env


@pytest.fixture(scope="module")
def cc_reference(tmp_path_factory) -> str:
    """What the program prints when built by the system C compiler."""
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no system C compiler found to act as the oracle")
    work = tmp_path_factory.mktemp("cc_oracle")
    src = work / "program.c"
    src.write_text(_PROGRAM, encoding="utf-8")
    exe = work / "program.cc.out"
    build = subprocess.run(
        [cc, str(src), "-o", str(exe)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, check=True
    )
    return run.stdout


def test_pcc1_compiles_and_links_c_without_a_host_pcc(tmp_path, cc_reference):
    assert PCC1 is not None, (
        "no pcc1 binary found; the session provisions one with "
        "scripts/bootstrap.sh --stage 1"
    )
    src = tmp_path / "program.c"
    src.write_text(_PROGRAM, encoding="utf-8")
    exe = tmp_path / "program.pcc1.out"
    build = subprocess.run(
        [str(PCC1), str(src), "-o", str(exe)],
        capture_output=True,
        text=True,
        timeout=900,
        env=_no_host_delegation_env(),
        cwd=str(tmp_path),
    )
    assert build.returncode == 0, (
        "pcc1 failed to compile C:\n" + build.stdout + build.stderr
    )
    assert "PCC-CPY-UNSUPPORTED-L3-TOOLING-C-DRIVER" not in (
        build.stdout + build.stderr
    ), build.stdout + build.stderr
    assert exe.is_file(), build.stdout + build.stderr
    run = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, check=True
    )
    assert run.stdout == cc_reference
    assert run.stdout.splitlines() == ["42", "12", "v0=20", "v1=22", "v2=42"]


def test_pcc1_reports_a_c_syntax_error_instead_of_delegating(tmp_path):
    """A rejected input must be pcc1's own diagnostic, not a delegation error."""
    assert PCC1 is not None
    src = tmp_path / "broken.c"
    src.write_text(
        textwrap.dedent(
            """
            int main(void) {
                return
            }
            """
        ),
        encoding="utf-8",
    )
    build = subprocess.run(
        [str(PCC1), str(src), "-o", str(tmp_path / "broken.out")],
        capture_output=True,
        text=True,
        timeout=300,
        env=_no_host_delegation_env(),
        cwd=str(tmp_path),
    )
    assert build.returncode != 0
    combined = build.stdout + build.stderr
    assert "PCC-CPY-UNSUPPORTED-L3-TOOLING-C-DRIVER" not in combined, combined
    assert "does-not-exist-host" not in combined, combined
