"""Pinned M2-NUMPY-L4 integration gate.

Proves `import numpy as np; print(np.__version__)` runs end-to-end through a
self-backend, no-libpython pcc1 binary against the pcc-native NumPy site, and
that the produced artifact links no libpython/host-Python edge.

Env-gated like the L5 gate so it never runs in the normal suite: set
``PCC_RUN_NUMPY_L4_INTEGRATION=1`` to enable. It reuses the prebuilt pcc-native
NumPy core site produced by the DONE_STRONG predecessor gates
(``build/head-truth/numpy-core/site``) and a self-host pcc1 binary
(``PCC1_BINARY`` env override, else ``build/bootstrap/pcc1``). When a
prerequisite is missing the test skips with an explicit reason rather than
fabricating success.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.pcc_gate(env="PCC_RUN_NUMPY_L4_INTEGRATION")]

REPO = Path(__file__).resolve().parents[2]
NUMPY_SITE = REPO / "build" / "head-truth" / "numpy-core" / "site"
NUMPY_ROOT = REPO / "projects" / "numpy-2.4.4"
MESON_BUILD = NUMPY_ROOT / "build" / "pcc-package" / "meson-build"
EXPECTED_VERSION = "2.4.4"


def _pcc1_binary() -> Path:
    env_path = os.environ.get("PCC1_BINARY")
    if env_path:
        return Path(env_path)
    return REPO / "build" / "bootstrap" / "pcc1"


def _require_l4_gate_enabled() -> Path:
    if os.environ.get("PCC_RUN_NUMPY_L4_INTEGRATION") != "1":
        pytest.fail("set PCC_RUN_NUMPY_L4_INTEGRATION=1 to run the real NumPy L4 pcc1 gate")
    pcc1 = _pcc1_binary()
    if not pcc1.is_file():
        pytest.fail(f"self-host pcc1 binary required: {pcc1} (set PCC1_BINARY or build via scripts/bootstrap.py)")
    if not (NUMPY_SITE / "numpy" / "_core").is_dir():
        pytest.fail(f"pcc-native NumPy core site required: {NUMPY_SITE} (run the M2-NUMPY predecessor gates)")
    return pcc1


def test_numpy_l4_import_and_version_through_pcc1_no_libpython(tmp_path):
    pcc1 = _require_l4_gate_enabled()

    main = tmp_path / "main.py"
    main.write_text("import numpy as np\nprint(np.__version__)\n", encoding="utf-8")
    app = tmp_path / "numpy_l4_app"

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = os.pathsep.join(
        [str(NUMPY_SITE), str(MESON_BUILD), str(NUMPY_ROOT)]
    )

    compile_proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(app),
        ],
        text=True,
        capture_output=True,
        timeout=600,
        env=env,
    )
    assert compile_proc.returncode == 0, (
        f"pcc1 numpy L4 compile failed:\n{compile_proc.stdout}\n{compile_proc.stderr}"
    )
    assert app.is_file(), "pcc1 produced no L4 binary"

    # Run isolated: no host Python, no package-site env, no host pcc edge.
    run_env = os.environ.copy()
    run_env.pop("LC_ALL", None)
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run_env["PYTHONPATH"] = ""
    run_env["PCC_PACKAGE_SITE"] = ""
    run_proc = subprocess.run(
        [str(app)], text=True, capture_output=True, timeout=120, env=run_env
    )
    assert run_proc.returncode == 0, (
        f"pcc1-built numpy binary exited {run_proc.returncode}:\n"
        f"{run_proc.stdout}\n{run_proc.stderr}"
    )
    assert run_proc.stdout.strip() == EXPECTED_VERSION, run_proc.stdout

    # The artifact must not link libpython / host CPython.
    otool = subprocess.run(
        ["otool", "-L", str(app)], text=True, capture_output=True, timeout=60
    )
    assert otool.returncode == 0, otool.stderr
    lowered = otool.stdout.lower()
    assert "libpython" not in lowered, otool.stdout
    assert "python3" not in lowered, otool.stdout
